import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import pytest

from core.backtest.models.cost_model import StockCostModel
from core.broker.rate_limiter import RateLimiter
from core.broker.tw.shioaji_account_query import ShioajiAccountQuery
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.after_close import AfterCloseRunner
from core.live.factory import make_trade_cost_estimator
from core.models import RealizedTradeSnapshot
from core.models.cost_config import CostConfig
from core.utils import Action

"""
盤後以券商已實現損益校正成本估算

券商不提供逐筆成交的費用（2026-09-22 模擬環境實測）：股票只給淨損益，
期貨有 `fee`／`tax` 但沒有委託序號。故以「一筆已平倉交易」為單位比對：
期貨直接比費用與稅，股票以本地開倉成交價算毛損益、減去券商淨損益推得實際成本。

fixture 的數字取自當天模擬環境的實際回傳：2330 買 2480、賣 2475，淨損益 -14,903；
TXFJ6 一口 48271 → 48264，fee 50、tax 193。
"""

TODAY: datetime.date = datetime.date(2026, 9, 22)
NOW: datetime.datetime = datetime.datetime(2026, 9, 22, 14, 30)


# === 券商查詢的正規化 ===
class ProfitLossApi:
    """回傳 2026-09-22 實測格式的已實現損益"""

    stock_account: str = "S"
    futopt_account: str = "F"

    def __init__(self) -> None:
        self.queries: List[Dict[str, Any]] = []

    def list_profit_loss(
        self, account: str, begin_date: str, end_date: str
    ) -> List[Any]:
        self.queries.append({"account": account, "begin": begin_date, "end": end_date})
        if account == "S":
            return [
                SimpleNamespace(
                    id=0,
                    code="2330",
                    quantity=1,
                    pnl=-14903.0,
                    date="20260922",
                    dseq="0FBDBC",
                    price=2475.0,
                    seqno="00B7ED",
                )
            ]
        return [
            SimpleNamespace(
                id=0,
                code="TXFJ6",
                quantity=1,
                pnl=-1400.0,
                date="20260922",
                entry_price=48271.0,
                cover_price=48264.0,
                tax=193,
                fee=50,
            )
        ]


def test_realized_trades_are_normalized_per_market() -> None:
    api: ProfitLossApi = ProfitLossApi()
    query: ShioajiAccountQuery = ShioajiAccountQuery(
        api, RateLimiter(), futures_symbol=lambda code: "TX202610"
    )

    trades: List[RealizedTradeSnapshot] = query.get_realized_trades(TODAY)

    stock, futures = trades
    assert (stock.symbol, stock.cover_price, stock.open_seqno) == (
        "2330",
        2475.0,
        "00B7ED",
    )
    assert (stock.fee, stock.tax, stock.entry_price) == (None, None, None)
    assert (futures.symbol, futures.fee, futures.tax, futures.entry_price) == (
        "TX202610",
        50.0,
        193.0,
        48271.0,
    )
    assert futures.is_futures and not stock.is_futures
    assert {q["begin"] for q in api.queries} == {"2026-09-22"}


# === 估算器 ===
def opening_fill(action: str, price: float, filled_at: str) -> Dict[str, Any]:
    return {"action": action, "price": price, "volume": 1, "filled_at": filled_at}


def stock_trade(cover_price: float = 2475.0) -> RealizedTradeSnapshot:
    return RealizedTradeSnapshot(
        symbol="2330",
        quantity=1,
        pnl=-14903.0,
        cover_price=cover_price,
        open_seqno="S1",
    )


def test_stock_long_is_taxed_on_the_cover_leg() -> None:
    """多單：證交稅課在平倉（賣出）那一腿；同一天開平倉用當沖稅率"""

    model: StockCostModel = StockCostModel(CostConfig.default())
    estimated: Optional[float] = make_trade_cost_estimator()(
        stock_trade(), [opening_fill("Buy", 2480.0, "2026-09-22T12:00:39+08:00")], TODAY
    )

    expected: int = (
        model.commission(2480.0, 1)
        + model.commission(2475.0, 1)
        + model.tax(2475.0, 1, Action.SELL, is_day_trade=True, date=TODAY)
    )
    assert estimated == float(expected)


def test_stock_short_is_taxed_on_the_opening_leg() -> None:
    """空單：賣出是開倉那一腿，稅課在開倉價"""

    model: StockCostModel = StockCostModel(CostConfig.default())
    estimated: Optional[float] = make_trade_cost_estimator()(
        stock_trade(cover_price=2470.0),
        [opening_fill("Sell", 2480.0, "2026-09-21T13:25:00+08:00")],
        TODAY,
    )

    expected: int = (
        model.commission(2480.0, 1)
        + model.commission(2470.0, 1)
        + model.tax(
            2480.0, 1, Action.SELL, is_day_trade=False, date=datetime.date(2026, 9, 21)
        )
    )
    assert estimated == float(expected)


def test_day_trade_tax_rate_applies_only_to_same_day_trades() -> None:
    estimator: Any = make_trade_cost_estimator()

    same_day: Optional[float] = estimator(
        stock_trade(), [opening_fill("Buy", 2480.0, "2026-09-22T09:30:00+08:00")], TODAY
    )
    overnight: Optional[float] = estimator(
        stock_trade(), [opening_fill("Buy", 2480.0, "2026-09-21T09:30:00+08:00")], TODAY
    )

    assert same_day is not None and overnight is not None
    assert same_day < overnight


def test_futures_estimate_covers_one_leg_like_the_broker() -> None:
    """
    期貨只估平倉那一腿，與券商欄位的範圍一致

    2026-09-22 實測：台指期一口來回的已實現紀錄 `fee=50`、`tax=193`，正好是單邊。
    照來回估會得到兩倍，每筆都被誤報 -50%。
    """

    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="TX202610",
        quantity=1,
        entry_price=48271.0,
        cover_price=48264.0,
        fee=50.0,
        tax=193.0,
        is_futures=True,
    )

    assert make_trade_cost_estimator()(trade, [], TODAY) == 243.0


def test_estimator_gives_up_without_what_it_needs() -> None:
    """缺開倉成交、或期貨乘數未知（股票期貨不在常數表）時回 None，不猜"""

    estimator: Any = make_trade_cost_estimator()
    stock_futures: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="CDF202610",
        quantity=1,
        entry_price=2400.0,
        cover_price=2410.0,
        fee=40.0,
        tax=10.0,
        is_futures=True,
    )

    assert estimator(stock_trade(), [], TODAY) is None
    assert estimator(stock_futures, [], TODAY) is None


# === 盤後比對 ===
@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


def make_runner(
    dao: LiveTradeDAO,
    trades: List[RealizedTradeSnapshot],
    estimate: Optional[float],
    root: Path,
) -> AfterCloseRunner:
    broker: Any = SimpleNamespace(get_realized_trades=lambda run_date: trades)
    reporter: Any = SimpleNamespace(output_root=root)
    return AfterCloseRunner(
        data_feeds=[],
        broker=broker,
        order_manager=None,
        account_sync=None,
        reconciler=None,
        reporter=reporter,
        mode_state=None,
        dao=dao,
        run_id="run9",
        now_provider=lambda: NOW,
        cost_estimator=lambda trade, opening, run_date: estimate,
    )


def seed_opening_fill(dao: LiveTradeDAO) -> None:
    """2330 開倉買進（序號 00B7ED，2480 一張）"""

    dao.insert_fill(
        {
            "broker_seqno": "00B7ED",
            "broker_trade_id": "00B7ED",
            "symbol": "2330",
            "action": "Buy",
            "price": 2480.0,
            "volume": 1,
            "filled_at": datetime.datetime(2026, 9, 22, 12, 0, 39),
        }
    )


def drift_events(dao: LiveTradeDAO) -> List[Any]:
    return dao.conn.execute(
        "SELECT symbol FROM live_risk_event WHERE category = 'COST_MODEL_DRIFT'"
    ).fetchall()


def test_stock_actual_cost_is_gross_minus_broker_pnl(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """(2475 − 2480) × 1 張 × 1000 − (−14,903) ＝ 9,903"""

    seed_opening_fill(dao)
    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="2330", quantity=1, pnl=-14903.0, cover_price=2475.0, open_seqno="00B7ED"
    )

    rows: List[Dict[str, Any]] = make_runner(
        dao, [trade], 9903.0, tmp_path
    ).calibrate_costs(TODAY)

    assert rows[0]["actual_cost"] == 9903.0
    assert rows[0]["diff"] == 0.0
    assert drift_events(dao) == []


def test_futures_actual_cost_is_broker_fee_plus_tax(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="TX202610",
        quantity=1,
        pnl=-1400.0,
        entry_price=48271.0,
        cover_price=48264.0,
        fee=50.0,
        tax=193.0,
        is_futures=True,
    )

    rows: List[Dict[str, Any]] = make_runner(
        dao, [trade], 243.0, tmp_path
    ).calibrate_costs(TODAY)

    assert rows[0]["actual_cost"] == 243.0


def test_large_gap_writes_a_drift_event_and_the_report(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """差距超過門檻寫事件；報表落在 `account/` 目錄"""

    seed_opening_fill(dao)
    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="2330", quantity=1, pnl=-14903.0, cover_price=2475.0, open_seqno="00B7ED"
    )

    make_runner(dao, [trade], 5000.0, tmp_path).calibrate_costs(TODAY)

    assert drift_events(dao) == [("2330",)]
    report: pd.DataFrame = pd.read_csv(
        tmp_path / "account" / "2026-09-22_cost_calibration.csv"
    )
    assert list(report["symbol"].astype(str)) == ["2330"]


def test_trade_without_local_opening_fill_is_reported_not_dropped(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    """
    開倉成交不在本地紀錄時照樣列出並註明原因

    略過的話報表看起來全都對得上，而那一筆其實根本沒比到。
    """

    trade: RealizedTradeSnapshot = RealizedTradeSnapshot(
        symbol="2317", quantity=1, pnl=-100.0, cover_price=250.0, open_seqno="UNKNOWN"
    )

    rows: List[Dict[str, Any]] = make_runner(
        dao, [trade], 500.0, tmp_path
    ).calibrate_costs(TODAY)

    assert rows[0]["actual_cost"] is None
    assert "開倉成交不在本地紀錄" in rows[0]["note"]
    assert drift_events(dao) == []


def test_missing_broker_query_skips_calibration(
    dao: LiveTradeDAO, tmp_path: Path
) -> None:
    runner: AfterCloseRunner = make_runner(dao, [], 0.0, tmp_path)
    runner.broker = SimpleNamespace()

    assert runner.calibrate_costs(TODAY) == []
