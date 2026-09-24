import datetime
from typing import Optional

from core.utils import Action, ExecutionTiming, OrderType, PositionType

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
        order_type: OrderType = OrderType.ROD,  # 委託效期（ROD / IOC / FOK）
        timing: Optional[ExecutionTiming] = None,  # 實盤執行時點；回測忽略
        client_order_id: Optional[str] = None,  # 本地委託識別碼；由 OMS 填入
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

        # === 實盤執行欄位（回測完全不讀，且全部有預設值）===
        #
        # 委託效期。回測是「當根 bar 撮合完就結束」，沒有留單的概念，
        # 故 ROD／IOC／FOK 的差別只在實盤成立
        self.order_type: OrderType = order_type

        # 這張單要在哪一個段落送出。
        #
        # `None` 表示「由引擎依策略的 `live_schedule` 決定」，策略不必逐單填。
        # 日 K 在實盤不存在——回測一次呼叫就拿到當日 OHLC，實盤在開盤前不知道
        # close、收盤前不知道完整 OHLC，所以同一支策略的鉤子要拆成兩個時點送單
        self.timing: Optional[ExecutionTiming] = timing

        # 本地委託識別碼（FIX 的 ClOrdID），**由 OMS 填入，策略不要填**。
        #
        # 它是「先寫 DB 再送單」能夠恢復的關鍵：程式在 `place_order()` 前後崩潰時，
        # 重啟後靠它到券商端精確比對，才不會因為「不知道送出去了沒」而重送
        self.client_order_id: Optional[str] = client_order_id
