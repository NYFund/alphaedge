import datetime

from core.utils import PositionType

"""BasePosition: 市場與商品皆無關的未平倉部位骨架（識別欄位一律為 symbol）"""


class BasePosition:
    """
    未平倉部位的共用骨架

    只收「每個市場都有」的欄位：識別、方向、開倉資訊（含不再變動的進場價）、
    交易成本、未實現損益、保證金與持有天數。借券費、股利補償等信用交易欄位屬台股專屬，
    由 StockPosition 補上；契約乘數與逐日結算損益屬期貨專屬，由 FuturesPosition 補上。
    """

    def __init__(
        self,
        id: int = 0,
        symbol: str = "",
        is_closed: bool = False,
        position_type: PositionType = PositionType.LONG,
        date: datetime.date = None,
        price: float = 0.0,
        volume: int = 0,
        commission: float = 0.0,
        tax: float = 0.0,
        transaction_cost: float = 0.0,
        unrealized_pnl: float = 0.0,
        unrealized_roi: float = 0.0,
        margin: float = 0.0,
        holding_days: int = 0,
    ) -> None:
        # Basic Info
        self.id: int = id  # 倉位編號（每筆倉位唯一編號）
        self.symbol: str = symbol  # 商品代號
        self.is_closed: bool = is_closed  # 是否已經平倉
        self.position_type: PositionType = position_type  # 持倉方向（Long or Short）

        # Position Info
        self.date: datetime.date = date  # 開倉日期
        self.price: float = price  # 開倉價位
        # 進場價，開倉後不再變動：有逐日結算的市場（期貨）會把 `price` 重設成結算價，
        # 已實現損益的進場價要從這裡取；台股兩者永遠相同
        self.entry_price: float = price
        self.volume: int = volume  # 開倉數量（台股為張、期貨為口）

        # Transaction Costs
        self.commission: float = commission  # 開倉手續費
        self.tax: float = tax  # 開倉交易稅
        self.transaction_cost: float = transaction_cost  # 開倉交易成本

        # Transaction Performance
        self.unrealized_pnl: float = unrealized_pnl  # 未實現損益
        self.unrealized_roi: float = unrealized_roi  # 未實現報酬率

        # Capital Usage & Holding
        # 已繳保證金（台股放空的保證金、期貨的原始保證金）；無保證金制度的部位為 0
        self.margin: float = margin
        self.holding_days: int = holding_days  # 已持有曆日數（＝當前日期 − 開倉日）
