import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides
from core.backtest.simulator.tick_simulator import TickSimulator
from core.utils import Scale, TickUniverse
from strategies.stock.intraday_momentum_strategy import IntradayMomentumStrategy
from tests.test_intraday_momentum_strategy import DAY_NEXT, DAY_T, make_strategy

"""
盤中動能策略的逐筆回測：同一份逐筆分支在 TickSimulator 上跑完觸發、停損、隔天開盤出場

平盤價 100：觸發價 109、停損價 107.5、累計量門檻 5,000 張（常數見 `test_intraday_momentum_strategy.py`）。
資金放大到一億，讓每檔的張數不會被算成 0；委買委賣量給足，成交量上限（10%）不截斷。
"""

DEEP_BOOK: int = 100_000


def _tick(
    day: datetime.date,
    clock: str,
    close: float,
    volume: int,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
) -> Dict:
    """2330 的一筆 tick；委買委賣預設在成交價上下各一檔（0.5）"""

    return {
        "stock_id": "2330",
        "time": pd.Timestamp(f"{day.isoformat()} {clock}"),
        "close": close,
        "volume": volume,
        "bid_price": close - 0.5 if bid is None else bid,
        "bid_volume": DEEP_BOOK,
        "ask_price": close if ask is None else ask,
        "ask_volume": DEEP_BOOK,
        "tick_type": 1,
    }


class _DatedTickAPI:
    """依日期與代號過濾的假 tick API，補上當日累計量"""

    def __init__(self, rows: List[Dict]) -> None:
        frame: pd.DataFrame = (
            pd.DataFrame(rows)
            .sort_values(["time"], kind="stable")
            .reset_index(drop=True)
        )
        frame["cum_volume"] = frame.groupby(
            [frame["stock_id"], frame["time"].dt.normalize()]
        )["volume"].cumsum()
        self.ticks: pd.DataFrame = frame

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        stock_ids: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        day: pd.Series = self.ticks["time"].dt.date
        selected: pd.DataFrame = self.ticks[(day >= start_date) & (day <= end_date)]
        if stock_ids is not None:
            selected = selected[selected["stock_id"].isin(stock_ids)]
        return selected.reset_index(drop=True)


def _run(rows: List[Dict], monkeypatch: pytest.MonkeyPatch) -> Backtester:
    """以 TICK 跑 DAY_T、DAY_NEXT 兩天（全市場模式）"""

    strategy: IntradayMomentumStrategy = make_strategy()
    strategy.init_capital = 100_000_000.0
    strategy.start_date = DAY_T
    strategy.end_date = DAY_NEXT

    monkeypatch.setattr(Backtester, "setup", lambda self: None)
    backtester: Backtester = build_backtester(
        strategy,
        write_artifacts=False,
        overrides=BacktestOverrides(scale=Scale.TICK, tick_universe=TickUniverse.ALL),
    )
    backtester.data_feed.tick = _DatedTickAPI(rows)
    monkeypatch.setattr(backtester.data_feed, "is_market_open", lambda date: True)
    monkeypatch.setattr(backtester.data_feed, "close", lambda: None)

    backtester.run()
    return backtester


def _closed(backtester: Backtester) -> List[Tuple[float, float]]:
    """已平倉的 (買價, 賣價)"""

    return [
        (r.buy_price, r.sell_price)
        for r in backtester.account.trade_records
        if r.is_closed
    ]


def test_supports_day_and_tick_with_day_as_default() -> None:
    """宣告支援 DAY 與 TICK，預設維持 DAY（既有的日 K 近似結果不變）"""

    strategy: IntradayMomentumStrategy = IntradayMomentumStrategy()

    assert strategy.scale == Scale.DAY
    assert strategy.get_supported_scales() == {Scale.DAY, Scale.TICK}


def test_entry_then_next_day_open_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    累計量達 5,000 張且現價達 109 才進場，下一筆以委賣價 110 買進；
    沒停損就留倉，隔天開盤段以第一筆（開盤集合競價）112 賣出
    """

    rows: List[Dict] = [
        _tick(DAY_T, "09:10:00", 109.0, 3000),  # 量不足
        _tick(DAY_T, "09:20:00", 109.0, 2500),  # 累計 5,500 張 → 進場
        _tick(DAY_T, "09:21:00", 109.5, 10, ask=110.0),  # 以委賣價 110 成交
        _tick(DAY_T, "10:00:00", 109.0, 10),
        _tick(DAY_T, "13:30:00", 109.5, 10),
        _tick(DAY_NEXT, "09:00:00", 112.0, 10),  # 開盤集合競價
        _tick(DAY_NEXT, "09:30:00", 113.0, 10),
    ]

    backtester: Backtester = _run(rows, monkeypatch)

    assert isinstance(backtester.simulator, TickSimulator)
    assert _closed(backtester) == [(110.0, 112.0)]


def test_same_day_stop_loss_sells_once_at_next_bid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """進場當天跌到 107.5 以下：下一筆以委買價賣出，之後再跌也不會重複送停損"""

    rows: List[Dict] = [
        _tick(DAY_T, "09:20:00", 109.0, 5500),
        _tick(DAY_T, "09:21:00", 109.5, 10, ask=110.0),
        _tick(DAY_T, "11:00:00", 107.0, 10),  # 跌破停損價
        _tick(DAY_T, "11:01:00", 107.0, 10, bid=106.5),  # 以委買價 106.5 賣出
        _tick(DAY_T, "11:02:00", 106.0, 10),
        _tick(DAY_T, "11:03:00", 105.5, 10),
    ]

    backtester: Backtester = _run(rows, monkeypatch)

    stages: List[str] = [stage for _, stage, _ in backtester.submitted_orders]
    assert _closed(backtester) == [(110.0, 106.5)]
    assert stages.count("stop_loss") == 1
