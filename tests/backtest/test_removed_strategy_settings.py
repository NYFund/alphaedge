import pytest

from core.backtest.factory import build_backtester
from core.strategies.base import RemovedStrategySettingError
from tests.backtest.conftest import ScriptedStrategy

"""
已移除的策略設定欄位：照舊寫法設定時要報錯，不能靜默忽略

Python 允許任意屬性指派，策略照舊文件設 `bar_execution_order` 不會有任何錯誤，
引擎卻完全不讀——策略作者以為設定生效，回測照預設值跑。
"""


@pytest.mark.parametrize(
    "name",
    [
        "position_type",
        "allowed_directions",
        "enable_intraday",
        "bar_execution_order",
        "is_intraday",
    ],
)
def test_removed_setting_is_reported(name: str) -> None:
    """每個已移除欄位都會被點名，並指出改用什麼"""

    strategy: ScriptedStrategy = ScriptedStrategy()
    setattr(strategy, name, object())

    problems = strategy.check_removed_settings()

    assert len(problems) == 1
    assert f"`{name}`" in problems[0]


def test_current_settings_are_not_reported() -> None:
    """只用現行欄位的策略不會被誤報"""

    assert ScriptedStrategy().check_removed_settings() == []


def test_backtest_refuses_to_start(monkeypatch) -> None:
    """回測在組裝任何元件之前就拒絕，訊息帶出策略名稱與欄位"""

    strategy: ScriptedStrategy = ScriptedStrategy()
    strategy.enable_intraday = True

    with pytest.raises(RemovedStrategySettingError, match="enable_intraday"):
        build_backtester(strategy)
