import datetime
from typing import Optional

from core.utils import Action, PositionType

"""BaseOrder: 市場與商品皆無關的訂單骨架（識別欄位一律為 symbol）"""


class BaseOrder:
    """
    買賣訂單的共用骨架

    方向（LONG／SHORT）與市場（股票／期貨）是兩條獨立的軸：
    position_type 屬前者，故留在本骨架；放空管道等台股專屬欄位由 StockOrder 補上。
    """

    def __init__(
        self,
        symbol: str = "",  # 商品代號
        date: datetime.datetime = None,  # 交易日期（Tick會是Timestamp）
        action: Action = Action.BUY,  # 訂單動作（Buy / Sell）
        position_type: PositionType = PositionType.LONG,  # 持倉方向（Long / Short）
        price: float = 0.0,  # 交易價位
        volume: int = 0,  # 交易數量（台股為張、期貨為口）
        reference_price: Optional[float] = None,  # 滑價前的委託價
    ) -> None:
        # Basic Info
        self.symbol: str = symbol
        self.date: datetime.datetime = date

        # Order Info
        self.action: Action = action
        self.position_type: PositionType = position_type
        self.price: float = price
        self.volume: int = volume

        # 滑價前的委託價；`None` 表示這張單沒有經過滑價調整。
        #
        # **為什麼要存在訂單上**：滑價成本＝（成交價 − 委託價）× 數量 × 計價單位，
        # 而這三項分散在不同層——委託價只有 `FillModel`／`SettlementModel` 知道
        # （它們回傳的是副本，原單不往下走），計價單位只有部位管理層知道
        # （期貨的乘數逐契約不同，`InstrumentSpec.to_units()` 拿不到商品）。
        # 把委託價掛在副本上，兩者才會在同一個地方碰頭。
        #
        # **這不是帳務欄位**：滑價是**內含在成交價裡**的，不像手續費與稅那樣
        # 另外從餘額扣一筆。它只用於統計，見 `BaseAccount.total_slippage_cost`。
        self.reference_price: Optional[float] = reference_price
