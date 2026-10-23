import subprocess
from typing import Set

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides, InvalidBacktestOverridesError
from core.strategies.base import UnsupportedScaleError
from core.utils import Scale, TickUniverse
from strategies.loader import StrategyLoader
from strategies.stock.volume_breakout_momentum_strategy import (
    VolumeBreakoutMomentumStrategy,
)
from tests.entry_sandbox import run_isolated

"""
回測級別的宣告與覆寫（`supported_scales`、`BacktestOverrides.scale`、`--scale`）

只寫了日 K 邏輯的策略吃到 tick 不會報錯、只會默默算錯，所以級別一律以策略的宣告為準：
`--scale` 只能選策略支援的，策略自己的 `scale` 與 `supported_scales` 矛盾也當場拒絕。
"""


class _TickDeclaredStrategy(VolumeBreakoutMomentumStrategy):
    """在 `super().__init__()` 之後才把級別改成 TICK 的策略（多數策略的寫法）"""

    def __init__(self) -> None:
        super().__init__()

        self.scale = Scale.TICK


@pytest.fixture
def no_data_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Backtester.__init__` 最後會 `setup()` 讀資料庫；這裡只驗組裝，不需要資料"""

    monkeypatch.setattr(Backtester, "setup", lambda self: None)


# === 宣告 ===
def test_supported_scales_default_to_the_declared_scale() -> None:
    """沒宣告 `supported_scales` 時只支援 `self.scale`"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()

    assert strategy.supported_scales is None
    assert strategy.get_supported_scales() == {Scale.DAY}


def test_default_follows_scale_set_after_base_init() -> None:
    """
    子類在 `super().__init__()` 之後才改 `scale`：預設的支援範圍要跟著它

    基底若在 `__init__` 寫死 `{self.scale}`，會留著基底的 DAY，宣告 TICK 的策略反而不支援 TICK。
    """

    strategy: _TickDeclaredStrategy = _TickDeclaredStrategy()

    assert strategy.get_supported_scales() == {Scale.TICK}
    strategy.validate_scale()


def test_contradicting_declaration_is_refused(no_data_setup: None) -> None:
    """`scale` 不在 `supported_scales` 內：不經覆寫的組裝路徑也當場拒絕，訊息列出支援的級別"""

    strategy: _TickDeclaredStrategy = _TickDeclaredStrategy()
    strategy.supported_scales = {Scale.DAY}

    with pytest.raises(UnsupportedScaleError, match="supported_scales（DAY）"):
        build_backtester(strategy, write_artifacts=False)


def test_contradicting_declaration_is_refused_even_with_a_supported_override() -> None:
    """
    宣告矛盾時，即使 `--scale` 選的是支援範圍內的級別也拒絕

    覆寫會蓋掉 `scale`，先覆寫再驗就看不出策略本身寫錯了。
    """

    strategy: _TickDeclaredStrategy = _TickDeclaredStrategy()
    strategy.supported_scales = {Scale.DAY}

    with pytest.raises(UnsupportedScaleError):
        BacktestOverrides(scale=Scale.DAY).apply_to(strategy)


# === 覆寫 ===
def test_override_outside_supported_scales_is_refused() -> None:
    """只支援 DAY 的策略要求 TICK：覆寫值不合法，訊息列出支援的級別，策略維持原狀"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()

    with pytest.raises(InvalidBacktestOverridesError, match="支援：DAY"):
        BacktestOverrides(scale=Scale.TICK).apply_to(strategy)

    assert strategy.scale == Scale.DAY


def test_override_inside_supported_scales_reaches_the_engine(
    no_data_setup: None,
) -> None:
    """支援多個級別的策略：覆寫寫回策略，組裝出來的引擎也是覆寫後的級別"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    supported: Set[Scale] = {Scale.DAY, Scale.TICK}
    strategy.supported_scales = supported

    backtester: Backtester = build_backtester(
        strategy,
        write_artifacts=False,
        # 這支策略沒有盤前清單，TICK 要明確選全市場（策略清單模式會拒絕）
        overrides=BacktestOverrides(scale=Scale.TICK, tick_universe=TickUniverse.ALL),
    )

    assert strategy.scale == Scale.TICK
    assert backtester.scale == Scale.TICK


def test_no_scale_override_keeps_the_declared_scale(no_data_setup: None) -> None:
    """不帶 `--scale` 時沿用策略的級別（回歸雙線的前提）"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()

    backtester: Backtester = build_backtester(
        strategy, write_artifacts=False, overrides=BacktestOverrides()
    )

    assert backtester.scale == Scale.DAY


# === 入口（子行程） ===
def test_unsupported_scale_flag_exits_with_usage_error() -> None:
    """`--scale tick`（小寫也收）對只支援 DAY 的策略：用法錯誤，stderr 說明支援哪些級別"""

    result: subprocess.CompletedProcess = run_isolated(
        [
            "-m",
            "apps.backtest",
            "--strategy",
            "VolumeBreakoutMomentumStrategy",
            "--scale",
            "tick",
        ]
    )

    assert result.returncode == 2
    assert "支援：DAY" in result.stderr


def test_unknown_scale_value_is_rejected_by_the_parser() -> None:
    """不存在的級別由 argparse 擋下，同樣是用法錯誤"""

    result: subprocess.CompletedProcess = run_isolated(
        [
            "-m",
            "apps.backtest",
            "--strategy",
            "VolumeBreakoutMomentumStrategy",
            "--scale",
            "week",
        ]
    )

    assert result.returncode == 2
    assert "--scale" in result.stderr


# === 既有策略的宣告 ===
# 宣告支援逐筆的策略；新增一支時要同時寫好逐筆分支並更新這裡，否則下方測試轉紅
TICK_CAPABLE_STRATEGIES: Set[str] = {"IntradayMomentumStrategy"}


@pytest.mark.parametrize(
    "name", sorted(StrategyLoader.load_strategies()), ids=lambda name: name
)
def test_registered_strategies_declare_their_scales(name: str) -> None:
    """
    每支策略的支援級別都是明確的：只寫了日 K 邏輯的策略要求 TICK 會當場報錯，
    宣告支援 TICK 的只有清單上那幾支
    """

    strategy = StrategyLoader.load_strategies()[name]()
    strategy.validate_scale()

    if name in TICK_CAPABLE_STRATEGIES:
        assert strategy.get_supported_scales() == {Scale.DAY, Scale.TICK}
        strategy.validate_scale(Scale.TICK)
    else:
        assert strategy.get_supported_scales() == {Scale(strategy.scale)}
        with pytest.raises(UnsupportedScaleError):
            strategy.validate_scale(Scale.TICK)
