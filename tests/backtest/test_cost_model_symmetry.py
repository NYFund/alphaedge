from typing import List

import pytest

from core.market.cost_model import BaseCostModel
from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.models import FuturesAccount
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.position.futures.position_manager import FuturesPositionManager
from core.utils import PositionType, Units

"""
成本模型的對稱性：損益與報酬率的公式只有基底一份，兩個市場只提供計價單位與資金基礎

原本台股與期貨各寫一份 `realized_pnl()`／`roi()`，期貨的部位管理層又再寫一份報酬率；
改了乘數或方向的處理時另外兩份不會跟著改，也不會有測試失敗。
"""

COST_MODELS: List[type] = [TwStockCostModel, TwFuturesCostModel]


@pytest.mark.parametrize("model_cls", COST_MODELS)
def test_pnl_and_roi_live_only_in_the_base(model_cls: type) -> None:
    """子類不可再覆寫損益與報酬率；覆寫等於又抄了一份公式"""

    for name in ("realized_pnl", "roi"):
        assert name not in model_cls.__dict__, f"{model_cls.__name__} 覆寫了 {name}()"
    assert not hasattr(model_cls, "roi_on_capital"), "資金效率改由分母決定，不另設方法"


def test_short_pnl_is_the_mirror_of_long() -> None:
    """同一組價格，放空的損益是做多的相反數；成本從兩邊都扣"""

    model: TwStockCostModel = TwStockCostModel(CostConfig.default())

    long_pnl: float = model.realized_pnl(
        PositionType.LONG, 100.0, 110.0, 2, unit_size=Units.LOT, cost=100
    )
    short_pnl: float = model.realized_pnl(
        PositionType.SHORT, 100.0, 110.0, 2, unit_size=Units.LOT, cost=100
    )

    assert long_pnl == 19900.0
    assert short_pnl == -20100.0


def test_unit_size_is_the_only_market_difference_in_pnl() -> None:
    """期貨把契約乘數當計價單位，走的是同一條公式"""

    model: TwFuturesCostModel = TwFuturesCostModel(FuturesCostConfig.default())

    assert (
        model.realized_pnl(
            PositionType.LONG, 18000.0, 18100.0, 2, unit_size=200, cost=300.0
        )
        == (18100.0 - 18000.0) * 200 * 2 - 300.0
    )


def test_roi_guards_a_non_positive_capital_base() -> None:
    """資金基礎不是正數時回 0，不拋除零（例如保證金比率設成 0）"""

    model: TwFuturesCostModel = TwFuturesCostModel()

    assert model.roi(1000.0, 0.0) == 0.0
    assert model.roi(1000.0, -1.0) == 0.0
    assert model.roi(1000.0, 20000.0) == 5.0


def test_stock_capital_bases() -> None:
    """台股的兩種分母：名目 ＝ 開倉價金 ＋ 開倉成本；佔用資金在放空時是保證金 ＋ 開倉成本"""

    model: TwStockCostModel = TwStockCostModel(CostConfig.default())

    assert model.notional_capital(100.0, 2, entry_cost=85) == 200085.0
    assert (
        model.occupied_capital(PositionType.LONG, 100.0, 2, entry_cost=85) == 200085.0
    )
    assert (
        model.occupied_capital(
            PositionType.SHORT, 100.0, 2, entry_cost=85, margin=90000
        )
        == 90085.0
    )


def test_futures_manager_has_no_private_roi_formula() -> None:
    """期貨部位管理層的報酬率委派給成本模型，不再自己留一份公式"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(1_000_000.0), TwFuturesCostModel()
    )

    assert not hasattr(manager, "calculate_roi")
    assert isinstance(manager.cost_model, BaseCostModel)


def test_pnl_and_roi_are_rounded_once_in_the_base() -> None:
    """
    損益與報酬率在基底各 round 一次（小數 2 位），呼叫端拿到的就是帳上的數字

    不 round 的話，`(100.1 − 100.03) × 1000` 這種算式會以 70.00000000000028 進帳，
    交易明細與歷史回歸會差到小數尾巴。
    """

    model: TwStockCostModel = TwStockCostModel(CostConfig.default())

    pnl: float = model.realized_pnl(
        PositionType.LONG, 100.03, 100.1, 1, unit_size=Units.LOT, cost=0
    )

    assert pnl == 70.0
    assert model.roi(100.0, 3.0) == 3333.33
