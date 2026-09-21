import datetime

import pytest

from core.backtest.backtester import IntradayScaleMismatchError
from core.backtest.factory import build_backtester
from core.strategies.base import BaseStrategy
from core.strategies.strategy_loader import StrategyLoader
from core.utils import Scale

"""
盤中逐筆策略的契約守門

同一個 `generate_open_signals(stock_quotes)`，實盤逐筆拿到**長度 1** 的 list，
`Scale.TICK` 回測卻**一次拿到整天**的 tick——兩邊的 list 語意根本不同。

需要橫斷面的邏輯（挑當下最強的前 N 檔）在回測裡看起來完全正常，上了實盤每次只
看得到一檔，訊號完全不同，**而且兩邊都跑得完、都不報錯**。那是最難查的一種錯，
故在建立引擎時就讓它現形。
"""


def make_strategy(is_intraday: bool, scale: str) -> BaseStrategy:
    """取一支真實策略再改旗標；不自造假策略，避免與真實基底漂移"""

    strategy: BaseStrategy = StrategyLoader.load_strategies()["MomentumStrategy1"]()
    strategy.is_intraday = is_intraday
    strategy.scale = scale
    strategy.start_date = datetime.date(2024, 1, 2)
    strategy.end_date = datetime.date(2024, 1, 2)
    return strategy


def test_default_strategy_is_not_intraday() -> None:
    """**預設關閉**：既有策略一支都不該因為新增這個旗標而改變行為"""

    registry = StrategyLoader.load_strategies()
    assert all(cls().is_intraday is False for cls in registry.values())


def test_intraday_strategy_refuses_tick_backtest() -> None:
    """宣告逐筆觸發的策略跑 `Scale.TICK` 回測要當場拒絕"""

    with pytest.raises(IntradayScaleMismatchError, match="語意不同"):
        build_backtester(make_strategy(is_intraday=True, scale=Scale.TICK))


def test_intraday_strategy_may_still_run_day_backtest() -> None:
    """
    改用 `Scale.DAY` 就放行

    擋的是「報價 list 語意不同」，不是「盤中策略不准回測」——
    要估量級的人明確改設定即可，只是結果不可當實盤預估。
    """

    assert build_backtester(make_strategy(is_intraday=True, scale=Scale.DAY))


def test_non_intraday_tick_backtest_is_untouched() -> None:
    """
    既有的 TICK 回測語意不動：沒宣告逐筆的策略不受這道守門影響

    **直接驗守門本身而不是建一個 TICK 引擎**：TICK 回測要 DolphinDB，
    沒裝的環境會在載入資料時就先炸掉，那樣這條測試驗到的是相依有沒有裝，
    不是守門有沒有放行。
    """

    backtester = build_backtester(make_strategy(is_intraday=False, scale=Scale.DAY))
    backtester.scale = Scale.TICK

    backtester._reject_intraday_tick_backtest()
