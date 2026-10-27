import datetime
import subprocess
from types import SimpleNamespace
from typing import List, Optional

import pandas as pd
import pytest

from core.backtest.backtester import Backtester, InvalidTickUniverseError
from core.backtest.datafeed.tw.stock_tick_datafeed import StockTickDataFeed
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides
from core.backtest.simulator.tick_universe import (
    has_live_symbols,
    resolve_strategy_symbols,
)
from core.utils import Scale, TickUniverse
from strategies.stock.volume_breakout_momentum_strategy import (
    VolumeBreakoutMomentumStrategy,
)
from tests.entry_sandbox import run_isolated

"""
TICK 回測的標的範圍（`--tick-universe {strategy,all}`）

策略清單模式照實盤盤前訂閱的規則：前一交易日呼叫 `get_live_symbols()`，再加上當時的持倉。
策略沒有清單時選這個模式要當場報錯，不能自動退回全市場（一天上百萬筆，使用者卻不知道）。
"""

DAY: datetime.date = datetime.date(2024, 5, 8)
PREVIOUS_DAY: datetime.date = datetime.date(2024, 5, 7)


class _TickStrategy(VolumeBreakoutMomentumStrategy):
    """宣告 TICK、沒有 `get_live_symbols()` 的策略"""

    def __init__(self) -> None:
        super().__init__()

        self.scale = Scale.TICK


class _TickStrategyWithList(_TickStrategy):
    """宣告 TICK、有盤前清單的策略；記下被呼叫的日期"""

    def __init__(self, symbols: Optional[List[str]] = None) -> None:
        super().__init__()

        self.symbols: Optional[List[str]] = (
            symbols if symbols is not None else ["2330", "2317"]
        )
        self.asked: List[datetime.date] = []

    def get_live_symbols(self, latest_date: datetime.date) -> Optional[List[str]]:
        self.asked.append(latest_date)
        return self.symbols


def _account(*held: str) -> SimpleNamespace:
    """只提供 `get_positions()`（未平倉部位）的帳戶替身"""

    positions = [SimpleNamespace(symbol=symbol) for symbol in held]
    return SimpleNamespace(get_positions=lambda: positions)


@pytest.fixture
def no_data_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Backtester.__init__` 最後會 `setup()` 讀資料庫；這裡只驗組裝，不需要資料"""

    monkeypatch.setattr(Backtester, "setup", lambda self: None)


# === 選項的合法性 ===
def test_day_backtest_refuses_tick_universe(no_data_setup: None) -> None:
    """DAY 回測帶了範圍：拒絕，避免以為選項有作用"""

    with pytest.raises(InvalidTickUniverseError, match="只在 TICK 回測有效"):
        build_backtester(
            VolumeBreakoutMomentumStrategy(),
            write_artifacts=False,
            overrides=BacktestOverrides(tick_universe=TickUniverse.ALL),
        )


def test_day_backtest_has_no_tick_universe(no_data_setup: None) -> None:
    """DAY 回測不帶範圍時維持 None"""

    backtester: Backtester = build_backtester(
        VolumeBreakoutMomentumStrategy(), write_artifacts=False
    )

    assert backtester.tick_universe is None


def test_tick_default_without_live_symbols_is_refused(no_data_setup: None) -> None:
    """TICK 預設策略清單；策略沒有清單時拒絕並提示改用全市場，不自動退回全市場"""

    with pytest.raises(InvalidTickUniverseError, match="--tick-universe all"):
        build_backtester(_TickStrategy(), write_artifacts=False)


def test_tick_all_works_without_live_symbols(no_data_setup: None) -> None:
    """選全市場時不需要策略清單"""

    backtester: Backtester = build_backtester(
        _TickStrategy(),
        write_artifacts=False,
        overrides=BacktestOverrides(tick_universe=TickUniverse.ALL),
    )

    assert backtester.tick_universe == TickUniverse.ALL


def test_tick_defaults_to_strategy_list(no_data_setup: None) -> None:
    """有清單的策略不帶選項時用策略清單（與實盤一致）"""

    backtester: Backtester = build_backtester(
        _TickStrategyWithList(), write_artifacts=False
    )

    assert backtester.tick_universe == TickUniverse.STRATEGY


def test_has_live_symbols_detects_override() -> None:
    """只有覆寫了 `get_live_symbols()` 的策略才算有清單"""

    assert not has_live_symbols(_TickStrategy())
    assert has_live_symbols(_TickStrategyWithList())


# === 每日標的 ===
def test_strategy_symbols_add_held_positions_outside_the_list() -> None:
    """清單在前，清單外的持倉依代號排序附在後面；清單內的持倉不重複"""

    strategy: _TickStrategyWithList = _TickStrategyWithList(["2330", "2317"])

    symbols: List[str] = resolve_strategy_symbols(
        strategy, _account("2603", "2317", "1101"), PREVIOUS_DAY
    )

    assert symbols == ["2330", "2317", "1101", "2603"]
    assert strategy.asked == [PREVIOUS_DAY]


def test_strategy_returning_none_is_refused() -> None:
    """有覆寫卻在某天回傳 None：沒有清單就不知道讀什麼，報錯而不是改讀全市場"""

    strategy: _TickStrategyWithList = _TickStrategyWithList()
    strategy.symbols = None

    with pytest.raises(ValueError, match="--tick-universe all"):
        resolve_strategy_symbols(strategy, _account(), PREVIOUS_DAY)


class _RecordingTickAPI:
    """記下查詢的代號清單，回傳空表的 tick API"""

    def __init__(self) -> None:
        self.calls: List[Optional[List[str]]] = []

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        stock_ids: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        self.calls.append(stock_ids)
        return pd.DataFrame(columns=["stock_id", "time", "volume", "cum_volume"])


def test_each_universe_reads_only_its_expected_symbols() -> None:
    """策略清單模式只查清單＋持倉（一般股）；全市場模式查詢不帶清單"""

    api: _RecordingTickAPI = _RecordingTickAPI()
    feed: StockTickDataFeed = StockTickDataFeed()
    feed.tick = api

    symbols: List[str] = resolve_strategy_symbols(
        _TickStrategyWithList(["2330", "0050"]), _account("1101"), PREVIOUS_DAY
    )
    feed.setup_day(DAY, symbols)
    feed.setup_day(DAY, None)

    assert api.calls == [["1101", "2330"], None]


# === 入口（子行程） ===
def test_tick_universe_flag_on_day_strategy_exits_with_usage_error() -> None:
    """日 K 策略帶 `--tick-universe`：用法錯誤（退出碼 2），stderr 說明只在 TICK 有效"""

    result: subprocess.CompletedProcess = run_isolated(
        [
            "-m",
            "apps.backtest",
            "--strategy",
            "VolumeBreakoutMomentumStrategy",
            "--tick-universe",
            "ALL",
        ]
    )

    assert result.returncode == 2
    assert "只在 TICK 回測有效" in result.stderr
