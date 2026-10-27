import datetime

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides
from core.backtest.simulator.bar_simulator import BarSimulator
from core.backtest.simulator.tick_simulator import TickSimulator
from core.strategies.base import BaseStrategy
from core.utils import Scale, TickUniverse
from strategies.loader import StrategyLoader

"""
盤中逐筆策略的回測契約

同一個 `generate_open_signals(stock_quotes)`，實盤逐筆拿到**長度 1** 的 list。
`Scale.TICK` 回測由 `TickSimulator` 逐筆重放、每次也只給一筆，兩邊是同一個契約，
所以宣告逐筆觸發的策略可以跑 TICK 回測（舊版整天一次給，只能拒絕）。
"""


@pytest.fixture(autouse=True)
def skip_dataset_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    建立引擎時不載入回測資料

    守門在 `Backtester.__init__` 一開始就判定，與資料無關；載入資料卻要開
    `tw_stock.db` 查交易日，沒有資料庫的環境（CI）會因此失敗，
    量到的變成「這台機器有沒有資料庫」而不是守門有沒有放行。
    """

    monkeypatch.setattr(Backtester, "load_datasets", lambda self: None)


def make_strategy(is_tick_triggered: bool, scale: str) -> BaseStrategy:
    """取一支真實策略再改旗標；不自造假策略，避免與真實基底漂移"""

    strategy: BaseStrategy = StrategyLoader.load_strategies()[
        "VolumeBreakoutMomentumStrategy"
    ]()
    strategy.is_tick_triggered = is_tick_triggered
    strategy.scale = scale
    strategy.start_date = datetime.date(2024, 1, 2)
    strategy.end_date = datetime.date(2024, 1, 2)
    return strategy


def test_default_strategy_is_not_intraday() -> None:
    """
    **預設關閉**：只有明確宣告為盤中逐筆的策略才開

    新增策略時忘了關、或既有策略被誤改成逐筆，這裡會多出一支而轉紅。
    """

    registry = StrategyLoader.load_strategies()
    tick_triggered = {
        name for name, cls in registry.items() if cls().is_tick_triggered is True
    }
    assert tick_triggered == {"IntradayMomentumStrategy"}


def test_intraday_strategy_runs_tick_backtest_on_tick_simulator() -> None:
    """宣告逐筆觸發的策略跑 `Scale.TICK` 回測：組出逐筆 simulator，不再拒絕"""

    backtester: Backtester = build_backtester(
        make_strategy(is_tick_triggered=True, scale=Scale.TICK),
        write_artifacts=False,
        overrides=BacktestOverrides(tick_universe=TickUniverse.ALL),
    )

    assert isinstance(backtester.simulator, TickSimulator)


def test_intraday_strategy_may_still_run_day_backtest() -> None:
    """改用 `Scale.DAY` 照樣可以跑日 K 回測（日 K 近似，結果不可當實盤預估）"""

    backtester: Backtester = build_backtester(
        make_strategy(is_tick_triggered=True, scale=Scale.DAY), write_artifacts=False
    )

    assert isinstance(backtester.simulator, BarSimulator)
