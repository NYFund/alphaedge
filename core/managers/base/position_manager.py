from abc import ABC, abstractmethod
from typing import List, Optional

from loguru import logger

from core.models import BaseAccount, BaseOrder, BasePosition, BaseTradeRecord
from core.utils import Action, PositionType

"""BasePositionManager: 市場與商品皆無關的部位管理骨架（FIFO 拆單與方向篩選）"""


class BasePositionManager(ABC):
    """
    Base Class of Position Manager

    FIFO 拆單、方向篩選與「平倉量不足時只平已有部位」的處理都與市場與商品皆無關，
    故收在此；單筆部位的記帳（成本攤提、損益公式）由各市場自行實作。
    """

    def __init__(self, account: BaseAccount) -> None:
        self.account: BaseAccount = account

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Position Manager"""
        pass

    @abstractmethod
    def open_position(self, order: BaseOrder) -> Optional[BasePosition]:
        """開倉；記帳方式由各市場自行實作"""
        pass

    @abstractmethod
    def close_single_position(
        self,
        position: BasePosition,
        order: BaseOrder,
        close_volume: int,
    ) -> Optional[BaseTradeRecord]:
        """
        - Description:
            平掉「單一部位的指定張數」並產生交易紀錄

            這是 FIFO 主幹唯一的市場差異點：成本攤提比例、稅費口徑、
            保證金釋回等全部在此，主幹不需要知道任何市場規則。
        - Parameters:
            - position: BasePosition
                被平倉的部位
            - order: BaseOrder
                平倉訂單
            - close_volume: int
                本次平倉數量
        - Return:
            - Optional[BaseTradeRecord]
                本次平倉產生的交易紀錄
        """
        pass

    @abstractmethod
    def settle_daily(self, position: BasePosition, settle_price: float) -> None:
        """
        - Description:
            每日結算的掛點

            股票為 no-op（開倉→持有→平倉才實現損益）；期貨日後實作為
            「結算損益進 balance、`position.price` 重設為結算價」。
            這是兩者語意差異的收容處，避免日後動到 FIFO 主幹。
        - Parameters:
            - position: BasePosition
                待結算的部位
            - settle_price: float
                當日結算價
        """
        pass

    def accrue_slippage_cost(
        self, order: BaseOrder, volume: int, unit_size: int
    ) -> None:
        """
        - Description:
            把這張單的滑價價差累計到帳戶

            **只是統計，不動餘額**：滑價是內含在成交價裡的，成交價已經比委託價
            差了，損益早就反映了它——再扣一次就是重複計算。手續費與稅則相反，
            那是真的另外從餘額扣的一筆錢。

            `reference_price` 為 `None` 代表這張單沒經過滑價調整（未啟用滑價，
            或不經 `FillModel`／`SettlementModel` 的純記憶體測試），此時不累計。
        - Parameters:
            - order: BaseOrder
                已成交的訂單（帶 `reference_price` 的副本）
            - volume: int
                本次成交的數量（台股為張、期貨為口；部分平倉時是該次的量）
            - unit_size: int
                一單位的計價單位數（台股 1,000 股、期貨為契約乘數）
        """

        reference: Optional[float] = getattr(order, "reference_price", None)
        if not reference or reference == order.price:
            return

        self.account.total_slippage_cost += round(
            abs(order.price - reference) * volume * unit_size, 2
        )

    def resolve_target_position_type(self, order: BaseOrder) -> PositionType:
        """平倉動作反推目標部位方向：賣出平多單、買進回補空單"""

        return PositionType.LONG if order.action == Action.SELL else PositionType.SHORT

    def close_position(self, order: BaseOrder) -> List[BaseTradeRecord]:
        """
        - Description:
            下單平倉（支援 FIFO 拆倉與部分平倉）

            主幹與市場與商品皆無關：依方向篩出該商品的未平倉部位，由最早開倉者依序
            平掉，單筆部位的記帳交給 `close_single_position()`。
        - Parameters:
            - order: BaseOrder
                目標商品的訂單資訊
        - Return:
            - close_records: List[BaseTradeRecord]
                實際被平倉的所有部位（可能為多筆）
        """

        close_records: List[BaseTradeRecord] = []

        # 從帳戶抓出所有該商品未平倉且同方向的部位（FIFO）
        target_position_type: PositionType = self.resolve_target_position_type(order)
        open_positions: List[BasePosition] = [
            p
            for p in self.account.positions
            if p.symbol == order.symbol
            and not p.is_closed
            and p.position_type == target_position_type
        ]

        # Calculate remaining close volume
        remaining_close_volume: int = order.volume

        for position in open_positions:
            if remaining_close_volume <= 0:
                break

            # 這筆 position 要平倉的數量
            close_volume: int = min(position.volume, remaining_close_volume)

            record: Optional[BaseTradeRecord] = self.close_single_position(
                position, order, close_volume
            )
            if record is None:
                continue

            close_records.append(record)
            remaining_close_volume -= close_volume

        if remaining_close_volume > 0:
            logger.warning(
                f"[Close Position] Not enough holdings to close {order.volume} lots of {order.symbol}, "
                f"only closed {order.volume - remaining_close_volume} lots"
            )
            # 📌 業界常見做法：
            # ✔ 不會在 close_position() 內自動開空單（避免混淆職責）
            # ✔ 僅記錄已平倉的部分，對剩餘張數給出警告或拋出錯誤
            # ✔ 是否將剩餘張數視為新開空單，由上層策略層決定
            # 👉 若要嚴格限制，可改為 raise ValueError("Insufficient holdings to close position")

        # Remove closed positions
        self.account.remove_closed_positions()

        return close_records
