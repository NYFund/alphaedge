import datetime
from typing import Iterator, List

import pytest
from loguru import logger

from core.backtest.models.cost_model import StockCostModel
from core.models.cost_config import CostConfig, ShortConstraint

"""尚未實作的 ShortConstraint 欄位防呆

設了限制卻不生效、又完全不吭聲，比功能沒做更危險——使用者會把回測結果
誤讀成「已經考慮過該限制」。本檔案釘住「要嘛警告、要嘛明確拒絕」。
"""


@pytest.fixture
def warnings() -> Iterator[List[str]]:
    """收集 loguru 的 WARNING 訊息（loguru 不走 logging，pytest 的 caplog 抓不到）"""

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda message: messages.append(message), level="WARNING", format="{message}"
    )

    yield messages

    logger.remove(sink_id)


def make_config(constraint: ShortConstraint) -> CostConfig:
    """建立帶有指定限制的成本設定"""

    config: CostConfig = CostConfig.default()
    config.short_constraint = constraint
    return config


def test_default_constraint_is_silent(warnings: List[str]) -> None:
    """預設值不得發出任何未實作警告，否則每次回測都在洗版"""

    StockCostModel(make_config(ShortConstraint()))

    assert "尚未實作" not in "".join(warnings)


def test_no_constraint_is_silent(warnings: List[str]) -> None:
    """未提供 short_constraint 時同樣不警告"""

    StockCostModel(CostConfig.default())

    assert "尚未實作" not in "".join(warnings)


def test_allow_below_reference_warns(warnings: List[str]) -> None:
    """關閉平盤下放空會被忽略，必須警告"""

    StockCostModel(make_config(ShortConstraint(allow_below_reference=False)))

    assert "allow_below_reference" in "".join(warnings)
    assert "尚未實作" in "".join(warnings)


def test_day_trade_whitelist_warns(warnings: List[str]) -> None:
    """設定每日可當沖清單會被忽略，必須警告"""

    StockCostModel(
        make_config(
            ShortConstraint(day_trade_whitelist={datetime.date(2024, 1, 2): {"2330"}})
        )
    )

    assert "day_trade_whitelist" in "".join(warnings)
    assert "尚未實作" in "".join(warnings)


def test_check_borrowable_no_longer_blocked(warnings: List[str]) -> None:
    """
    券源檢核已於 2026-08-15 接上呼叫端，不再擋下也不再被標為未實作

    呼叫端為 `TwStockFillModel.check_short_borrowable()`；本測試只確認
    「建構不再拋錯、也不再警告未實作」，實際檢核行為見 `test_fill_model.py`
    """

    StockCostModel(make_config(ShortConstraint(check_borrowable=True)))

    assert not any("check_borrowable" in w for w in warnings)


def test_implemented_constraints_are_not_flagged(warnings: List[str]) -> None:
    """已接上呼叫端的兩個欄位不可被誤標為未實作"""

    StockCostModel(
        make_config(
            ShortConstraint(
                force_cover_dates={"2330": [datetime.date(2024, 1, 5)]},
                max_short_exposure_ratio=0.05,
            )
        )
    )

    assert "force_cover_dates" not in "".join(warnings)
    assert "max_short_exposure_ratio" not in "".join(warnings)
