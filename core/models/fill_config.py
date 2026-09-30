from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from core.utils import Action

"""
成交假設設定：滑價、成交量上限與超量處理方式

**與成交模型本體分開**：`TwStockFillModel`／`TwFuturesFillModel` 是回測引擎的可插拔 model，
而這幾個類別只是參數容器，由策略宣告、由回測組裝層讀取。與 `core/models/cost_config.py`
同一個理由：放在 `core/backtest/models/` 的話，策略基底就得 import 回測套件。
"""


class VolumeCapPolicy(str, Enum):
    """超過成交量上限時的處理方式"""

    TRUNCATE = "TRUNCATE"  # 縮量到上限（預設，較貼近實務）
    REJECT = "REJECT"  # 整張拒單


@dataclass
class FillConfig:
    """
    成交假設設定

    **與成交模型本體分開**（對照 `CostConfig` 之於 `StockCostModel`）：策略基底要宣告它，
    放在 `core/backtest/models/` 的話，策略層為了拿一個 dataclass 就得 import 回測套件。
    使用它的模擬邏輯在 `core/backtest/models/fill_model.py`。

    語意上與「法規費率」（`Commission`）分離：前者是可調的模擬參數，
    後者是外部給定的規則。

    全部預設為關閉，此時不改動任何訂單的價量。
    """

    # 滑價基點（1 bps = 0.01%）；買進加價、賣出減價
    slippage_bps_buy: float = 0.0
    slippage_bps_sell: float = 0.0

    # 單筆訂單張數不得超過當日成交量的比例；None 為關閉
    max_volume_share: Optional[float] = None

    # 超量時縮量或拒單
    volume_cap_policy: VolumeCapPolicy = VolumeCapPolicy.TRUNCATE


@dataclass
class FuturesFillConfig(FillConfig):
    """
    期貨的成交假設：**滑價改以跳動點（tick）表達**

    為什麼不沿用基點：期貨的價差報價本來就是「幾檔」，而同一個基點數在不同價位
    換算出的檔數不同——TX 在 12,000 點時 1 bps 是 1.2 點、在 24,000 點時是 2.4 點，
    同一組設定跨年份回測會靜默變成不同的滑價假設。

    **大台與小台要分開設**：MTX 的價差與成交量都與 TX 不同，
    用同一個數字會低估小台的成本，故提供 `slippage_ticks_by_product`。

    `slippage_ticks_*` 為 0 且未逐商品指定時，退回基底的基點設定（同樣預設關閉），
    行為與未啟用任何假設時完全相同。
    """

    slippage_ticks_buy: float = 0.0  # 買進滑價（跳動點數）
    slippage_ticks_sell: float = 0.0  # 賣出滑價（跳動點數）
    # 逐商品的滑價跳動點數；未列的商品沿用上面兩個共用值
    slippage_ticks_by_product: Optional[Dict[str, float]] = None

    def get_slippage_ticks(
        self, action: Action, product: Optional[str] = None
    ) -> float:
        """取得該商品該方向的滑價跳動點數；逐商品設定優先"""

        if product and self.slippage_ticks_by_product:
            ticks: Optional[float] = self.slippage_ticks_by_product.get(product)
            if ticks is not None:
                return ticks

        return (
            self.slippage_ticks_buy
            if action == Action.BUY
            else self.slippage_ticks_sell
        )
