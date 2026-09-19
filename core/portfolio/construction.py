import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple

from loguru import logger

from core.managers.futures.position_manager import FuturesMarginConfig
from core.models import (
    BaseAccount,
    BaseOrder,
    BaseQuote,
    FuturesOrder,
    FuturesQuote,
    StockOrder,
)
from core.portfolio.signal import Signal
from core.portfolio.sizing import BasePositionSizer

"""
部位建構：把開倉訊號換算成訂單（`List[Signal]` → `List[BaseOrder]`）

**只做開倉。** 平倉與停損的數量取自持倉、價格由策略的交易邏輯決定
（哪一筆部位、合併與否、回補價怎麼挑），那是部位查詢不是部位建構，
留在策略基底，見 `core/strategies/base.py` 的 `build_close_orders()`。

股票與期貨是**兩種不同的約束**，不可共用一個公式：
- 股票：用多少錢買多少股 → 資金切分（`BasePositionSizer`）
- 期貨：繳多少保證金開幾口 → 保證金約束（`FuturesPortfolioConstructor`）

**這些類別刻意不快取在策略身上**：策略的 `max_holdings`／`max_lots` 在
`__init__` 呼叫 `super().__init__()` **之後**才填，`margin_config` 更是由
`core/backtest/factory.py` 在策略建構完成後才注入。在基底 `__init__` 建一次
就會永遠拿到 `None`／`0`——而症狀是部位大小整段偏掉，不會有任何錯誤訊息。
故由策略的 `make_portfolio_constructor()` 每次組裝時重建。
"""


def normalize_quote_date(date) -> datetime.date:
    """Tick 級別的報價日期會是 datetime，統一取其日期部分"""

    return date.date() if isinstance(date, datetime.datetime) else date


class BasePortfolioConstructor(ABC):
    """
    開倉組裝的共用介面

    兩個實作的約束完全不同（資金切分／保證金），但對呼叫端是同一件事：
    「這些訊號各要下多少」。換配置演算法時新增實作即可，策略只需覆寫
    `make_portfolio_constructor()`。
    """

    @abstractmethod
    def build(self, signals: List[Signal], account: BaseAccount) -> List[BaseOrder]:
        """
        - Description:
            把開倉訊號換算成訂單
        - Parameters:
            - signals: List[Signal]
                Alpha 層選出的開倉訊號；`volume` 一律為 `None`
            - account: BaseAccount
                虛擬帳戶，提供餘額與現有持倉
        - Return:
            - List[BaseOrder]
                開倉訂單；數量不足最小單位者不回傳
        """

        pass


class StockPortfolioConstructor(BasePortfolioConstructor):
    """
    台股開倉：等權資金切分

    公式與取整規則全部由 `sizer` 決定，本類別只負責把訊號攤成 sizer 要的候選、
    再把回傳的張數組回訂單。**參考價由策略在 `Signal.sizing_price` 給定**，
    本層不代為選擇（見 `sizing.py` 的責任邊界）。
    """

    def __init__(
        self,
        sizer: BasePositionSizer,
        max_holdings: Optional[int] = None,
    ) -> None:
        self.sizer: BasePositionSizer = sizer  # 部位大小模型
        self.max_holdings: Optional[int] = max_holdings  # 最大持倉檔數；None 為不限制

    def build(self, signals: List[Signal], account: BaseAccount) -> List[StockOrder]:
        """依剩餘可開倉名額均分餘額並換算張數"""

        if not signals:
            return []

        candidates: List[Tuple[BaseQuote, float]] = []
        # sizer 回傳的是候選的子集（同一批 quote 物件），要靠它反查原訊號的方向與下單價。
        # 以 `id()` 為鍵而非 symbol：同一標的理論上只會有一個開倉訊號，但用 symbol
        # 當鍵的話，哪天真的出現兩個就會靜默覆蓋掉其中一個
        signal_by_quote: Dict[int, Signal] = {}

        for signal in signals:
            if signal.sizing_price is None:
                raise ValueError(
                    f"{signal.symbol} 的開倉訊號沒有 sizing_price，無法計算張數"
                )
            candidates.append((signal.quote, signal.sizing_price))
            signal_by_quote[id(signal.quote)] = signal

        orders: List[StockOrder] = []
        for quote, _, volume in self.sizer.size(account, candidates, self.max_holdings):
            signal: Signal = signal_by_quote[id(quote)]
            orders.append(
                StockOrder(
                    stock_id=signal.symbol,
                    date=quote.date,
                    action=signal.action,
                    position_type=signal.position_type,
                    price=signal.order_price,
                    volume=volume,
                )
            )
        return orders


class FuturesPortfolioConstructor(BasePortfolioConstructor):
    """
    台期貨開倉：保證金約束

    **不是資金切分**：拿契約價值去除可動用餘額會嚴重低估可開口數
    （TX 一口契約價值 900 萬、保證金只有 70 萬）。

    另有一層總口數上限：`max_lots` 扣掉帳上已開口數就是本次的可用額度，
    逐筆遞減、歸零即停。
    """

    def __init__(
        self,
        max_lots: int = 0,
        max_capital_usage: float = 0.5,
        margin_config: Optional[FuturesMarginConfig] = None,
        log_context: str = "",
    ) -> None:
        self.max_lots: int = max_lots  # 總口數上限（0 表示不開倉）
        self.max_capital_usage: float = max_capital_usage  # 可動用餘額的使用上限
        self.margin_config: Optional[FuturesMarginConfig] = margin_config
        self.log_context: str = log_context  # 記在 log 前綴的策略名稱

    def build(self, signals: List[Signal], account: BaseAccount) -> List[FuturesOrder]:
        """依保證金與剩餘口數上限計算下單口數"""

        if not signals:
            return []

        remaining_lots: int = self.max_lots - sum(
            abs(lots) for lots in account.get_open_lots().values()
        )

        orders: List[FuturesOrder] = []
        for signal in signals:
            if remaining_lots <= 0:
                break

            quote: FuturesQuote = signal.quote
            affordable: int = self.calculate_max_lots(quote, account)
            volume: int = min(affordable, remaining_lots)
            if volume <= 0:
                logger.info(
                    f"[{self.log_context}] {quote.contract_id} "
                    f"保證金不足或已達口數上限，跳過"
                )
                continue

            orders.append(
                FuturesOrder(
                    product=quote.product,
                    expiry=quote.expiry,
                    date=quote.date,
                    action=signal.action,
                    position_type=signal.position_type,
                    price=signal.order_price,
                    volume=volume,
                )
            )
            remaining_lots -= volume

        return orders

    def calculate_max_lots(self, quote: FuturesQuote, account: BaseAccount) -> int:
        """
        - Description:
            以**保證金**算出這筆訂單最多能開幾口

            股票是「用多少錢買多少股」，期貨是「繳多少保證金開幾口」。
            保證金取得方式與 `FuturesPositionManager` 一致：帶了 API 就查表，
            否則用比率近似。**查表查不到會往外拋**，那是刻意的，見
            `FuturesMarginConfig` 的說明。
        - Parameters:
            - quote: FuturesQuote
                目標契約的報價
            - account: BaseAccount
                虛擬帳戶，提供可動用餘額
        - Return:
            - int
                可開口數；帳戶或報價不足以計算時為 0
        """

        if account is None or quote.multiplier <= 0:
            return 0

        margin_per_lot: float = self.get_margin_per_lot(quote)
        if margin_per_lot <= 0:
            return 0

        budget: float = account.balance * self.max_capital_usage
        return max(0, int(budget // margin_per_lot))

    def get_margin_per_lot(self, quote: FuturesQuote) -> float:
        """
        取得每口原始保證金

        沒有 `margin_config` 或沒有 API 時退回「契約價值 × 比率」，
        與 `FuturesPositionManager` 的比率模式一致——兩處若不一致，
        算出來的口數會開不進去（或開得太少）。
        """

        config: FuturesMarginConfig = (
            self.margin_config or FuturesMarginConfig.default()
        )

        if config.api is not None:
            per_lot: Optional[int] = config.api.get_initial_margin(
                quote.product,
                normalize_quote_date(quote.date),
                fallback_to_earliest=config.fallback_to_earliest,
            )
            if per_lot is None:
                logger.warning(
                    f"[{self.log_context}] 查無 {quote.product} 在 {quote.date} "
                    f"的保證金，本次不開倉"
                )
                return 0.0
            return float(per_lot)

        return quote.close * quote.multiplier * config.initial_margin_ratio
