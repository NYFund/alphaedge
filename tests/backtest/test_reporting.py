import datetime
from pathlib import Path
from typing import Callable, Dict, List

import pandas as pd
import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.report.reporter import StockBacktestReporter
from core.dao.base import BaseDAO
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.models import StockAccount, StockOrder, StockTradeRecord
from core.utils import Action, PositionType, ShortMethod

"""每日權益、多空分開統計與事件報表的測試"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester"""

    def _make_backtester(strategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


def test_snapshot_daily_equity_includes_unrealized(
    make_strategy, make_backtester, make_quote
) -> None:
    """留倉放空的帳面虧損必須反映在每日權益，不能等到平倉才出現"""

    strategy = make_strategy(
        position_type=PositionType.SHORT,
        enable_intraday=False,
        short_method=ShortMethod.MARGIN,
        open_script={
            DAY_1: [
                StockOrder(
                    stock_id="2330",
                    date=DAY_1,
                    action=Action.SELL,
                    position_type=PositionType.SHORT,
                    price=100.0,
                    volume=1,
                )
            ]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(
        DAY_1, [make_quote(date=DAY_1, cur_price=100.0, high=101.0, low=99.0)]
    )

    # 開倉當日：現金 1000000 − 90422 + 部位（保證金 90000 + 未實現 0）
    assert backtester.daily_equity[0]["Equity"] == 999578.0

    # 次日股價上漲 5 元，未實現虧損 5000 應立刻反映在權益上
    day_2: datetime.date = datetime.date(2024, 1, 3)
    backtester.execute_bar(
        day_2, [make_quote(date=day_2, cur_price=105.0, high=106.0, low=104.0)]
    )

    assert backtester.account.get_positions()[0].unrealized_pnl == -5000.0
    assert backtester.daily_equity[1]["Equity"] == 994578.0
    assert backtester.account.realized_pnl == 0.0  # 尚未平倉，已實現損益仍為 0


def test_trading_report_columns_and_symbol(
    make_strategy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    報表的欄位名與識別欄位取值

    引擎內部改用 symbol 之後，輸出欄位名必須維持 `Stock ID`——
    改名會讓 915 筆 LONG baseline 失效。但回歸雙線是自行從 trade_records 組表的，
    完全不經過 reporter，故欄位名與取值只能靠本測試把關。
    """

    monkeypatch.setattr(StockBacktestReporter, "setup", lambda self: None)

    strategy = make_strategy(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
    )
    account: StockAccount = StockAccount(1000000.0)
    strategy.setup_account(account)

    account.trade_records.append(
        StockTradeRecord(
            id=1,
            stock_id="2330",
            is_closed=True,
            position_type=PositionType.LONG,
            buy_date=DAY_1,
            buy_price=100.0,
            buy_volume=1,
            sell_date=datetime.date(2024, 1, 5),
            sell_price=105.0,
            sell_volume=1,
            realized_pnl=4548.0,
            roi=4.53,
        )
    )

    reporter: StockBacktestReporter = StockBacktestReporter(strategy, tmp_path)
    reporter.account = account
    report: pd.DataFrame = reporter.generate_trading_report()

    # 欄位名維持台股語意，且順序不變（baseline 逐欄比對依賴此順序）
    assert list(report.columns)[:3] == ["Stock ID", "Position Type", "Entry Date"]
    assert "Symbol" not in report.columns

    # 取值來自 model 的 symbol，不是空字串
    assert report.loc[0, "Stock ID"] == "2330"
    assert report.loc[0, "Stock ID"] == account.trade_records[0].symbol


def test_direction_summary_and_event_report(
    make_strategy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多空分開統計與事件計數需正確輸出"""

    monkeypatch.setattr(StockBacktestReporter, "setup", lambda self: None)

    strategy = make_strategy(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
    )
    account: StockAccount = StockAccount(1000000.0)
    strategy.setup_account(account)

    account.trade_records.append(
        StockTradeRecord(
            id=1,
            stock_id="2330",
            is_closed=True,
            position_type=PositionType.SHORT,
            short_method=ShortMethod.MARGIN,
            sell_date=DAY_1,
            sell_price=100.0,
            buy_date=datetime.date(2024, 1, 12),
            buy_price=95.0,
            commission=82.0,
            tax=300.0,
            borrow_fee=80.0,
            interest=10.0,
            margin=90000.0,
            holding_days=10,
            realized_pnl=4548.0,
            roi=4.53,
            roi_on_capital=5.03,
        )
    )
    account.trade_records.append(
        StockTradeRecord(
            id=2,
            stock_id="2317",
            is_closed=True,
            position_type=PositionType.LONG,
            buy_date=DAY_1,
            buy_price=50.0,
            sell_date=datetime.date(2024, 1, 5),
            sell_price=48.0,
            commission=40.0,
            tax=144.0,
            realized_pnl=-2184.0,
            roi=-4.37,
        )
    )

    reporter: StockBacktestReporter = StockBacktestReporter(strategy, tmp_path)
    reporter.account = account
    reporter.trading_report = reporter.generate_trading_report()

    summary: pd.DataFrame = reporter.generate_direction_summary()
    short_row: pd.Series = summary[summary["Position Type"] == "SHORT"].iloc[0]
    long_row: pd.Series = summary[summary["Position Type"] == "LONG"].iloc[0]

    assert short_row["Trades"] == 1
    assert short_row["Win Rate (%)"] == 100.0
    assert short_row["Total Borrow Fee"] == 80.0
    assert short_row["Total Interest"] == 10.0
    assert short_row["Avg Holding Days"] == 10.0
    assert long_row["Total PnL"] == -2184.0
    assert long_row["Total Borrow Fee"] == 0.0

    events: Dict[str, int] = {"forced_cover_day_trade": 3, "limit_up_cover_failed": 1}
    event_df: pd.DataFrame = reporter.generate_event_report(events)

    assert set(event_df["Event"]) == set(events.keys())
    assert event_df[event_df["Event"] == "limit_up_cover_failed"]["Count"].iloc[0] == 1


# === 分割調整：已由 corporate_action 經還原係數統一處理（還原價 S3）===
def test_adjustment_factor_covers_splits_and_reductions(
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    分割與減資都要進累乘係數，不再靠一份只認得 0050 的過渡表

    `core/api/tw/stock_split.py` 於 2026-09-13 刪除。在那之前，還原係數只認
    除權息（`stock_dividend` 不含分割），所以 reporter 得自己
    再套一次分割調整才拿得到正確的 benchmark；`corporate_action` 表上線後
    改由 `get_adjusted_close_series()` 一次處理完。
    """

    from core.api.tw.stock_dividend_api import StockDividendAPI

    dividend_dao: BaseDAO = dao_factory(
        StockDividendDAO,
        records=[{"date": "2025-03-01", "stock_id": "0050", "還原係數": 0.98}],
    )
    dao_factory(
        CorporateActionDAO,
        records=[
            {
                "date": "2025-06-18",
                "stock_id": "0050",
                "調整倍率": 0.25,  # 一拆四：價格變四分之一
            }
        ],
    )

    api: StockDividendAPI = StockDividendAPI(conn=dividend_dao.conn)

    before: float = api.get_cumulative_factor("0050", datetime.date(2025, 6, 10))
    after: float = api.get_cumulative_factor("0050", datetime.date(2025, 6, 18))

    # 分割後的係數應為分割前的 4 倍（1 / 0.25），才能把 −75% 的假跌幅補回來
    assert after / before == pytest.approx(4.0)


def test_reporter_no_longer_double_adjusts() -> None:
    """
    reporter 不可再對還原價套一次分割調整

    **重複調整實測會讓 0050 的分割日由 1.82% 變成 303%**。這條釘住
    `_get_adjusted_price()` 已退化為原樣回傳——它保留只是為了讓兩處呼叫端
    不必各自改。
    """

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    raw: pd.Series = pd.Series(
        [188.65, 47.57],
        index=[datetime.date(2025, 6, 10), datetime.date(2025, 6, 18)],
    )

    assert reporter._get_adjusted_price(raw, "0050").equals(raw)


def test_transitional_split_table_is_gone() -> None:
    """
    過渡表已刪除，不得有人再 import 它

    留著會出現兩份分割來源，而抄漏一次分割的代價是整段序列從那天起錯 N 倍。
    """

    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.api.tw.stock_split")


# === reporter 可維護性===
def test_benchmark_uses_adjusted_close() -> None:
    """
    benchmark 取的是**還原**收盤價，不是原始收盤價

    0050 年年配息，用原始價當基準等於讓基準每年少賺一次配息，策略看起來
    永遠贏得比實際多。
    """

    import datetime as dt

    calls: List[str] = []

    class _Price:
        def get_adjusted_close_series(self, stock_id, start_date, end_date):
            calls.append("adjusted")
            return pd.Series(
                [100.0, 110.0],
                index=[dt.date(2024, 1, 2), dt.date(2024, 1, 3)],
            )

        def get_stock_price(self, *args, **kwargs):  # pragma: no cover - 不該被呼叫
            calls.append("raw")
            return pd.DataFrame()

        def close(self) -> None:
            calls.append("closed")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    reporter.price = _Price()
    reporter.benchmark = "0050"
    reporter.start_date = dt.date(2024, 1, 1)
    reporter.end_date = dt.date(2024, 1, 31)
    reporter.setup()

    assert calls == ["adjusted"]
    assert list(reporter.benchmark_price) == [100.0, 110.0]


def test_reporter_close_releases_only_its_own_connection() -> None:
    """
    共用連線不歸 reporter 關

    `StockPriceAPI` 以 `owns_conn` 區分；reporter 只是把 `close()` 轉發過去，
    自己不判斷。判斷寫兩份就會有一份漏掉。
    """

    closed: List[str] = []

    class _Price:
        def close(self) -> None:
            closed.append("price")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    reporter.price = _Price()
    reporter.close()

    assert closed == ["price"]

    # 期貨報表不建 StockPriceAPI（`setup()` 把 price 設成 None），不得炸
    reporter.price = None
    reporter.close()


def test_show_figures_defaults_to_off(monkeypatch) -> None:
    """
    預設不開瀏覽器

    reporter 有五張圖，舊版 `set_figure_config(show=True)` 寫死，每跑一次回測
    就彈出 5 個分頁；批次掃參數時一次開幾十個，無頭環境更是直接失敗。
    """

    from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures

    monkeypatch.delenv(SHOW_FIGURES_ENV_VAR, raising=False)
    assert resolve_show_figures() is False

    monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, "1")
    assert resolve_show_figures() is True


def test_show_figures_only_accepts_explicit_truthy(monkeypatch) -> None:
    """
    `ALPHAEDGE_SHOW_FIGURES=0` 是關，不是「有設就開」

    習慣寫 `VAR=0` 關功能的人踩到反效果，會是最難查的那種問題。
    """

    from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures

    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, value)
        assert resolve_show_figures() is False, value

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, value)
        assert resolve_show_figures() is True, value


def test_set_figure_config_does_not_open_browser_by_default() -> None:
    """`show` 不指定時跟隨 reporter 的設定，不是寫死 True"""

    import plotly.graph_objects as go

    opened: List[str] = []

    class _Figure(go.Figure):
        def show(self, *args, **kwargs) -> None:
            opened.append("shown")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    reporter.show = False
    reporter.set_figure_config(_Figure(), title="t")
    assert opened == []

    reporter.show = True
    reporter.set_figure_config(_Figure(), title="t")
    assert opened == ["shown"]
