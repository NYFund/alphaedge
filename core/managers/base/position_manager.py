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

            股票為 no-op（開倉→持有→平倉才實現損益）；期貨逐日盯市，
            結算損益當天就進 balance、`position.price` 重設為結算價。
            兩者的語意差異收在這個掛點，FIFO 主幹不必知道。
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

        # 依開倉先後取同方向的未平倉部位，最早開倉者先平（FIFO）
        target_position_type: PositionType = self.resolve_target_position_type(order)
        open_positions: List[BasePosition] = [
            p
            for p in self.account.positions
            if p.symbol == order.symbol
            and not p.is_closed
            and p.position_type == target_position_type
        ]

        remaining_close_volume: int = order.volume

        for position in open_positions:
            if remaining_close_volume <= 0:
                break

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
            # **刻意只警告、不 raise，也不把剩餘量自動轉成反向新倉**：
            # 平倉與開倉是兩種決策，要不要反手做空屬於策略層的判斷；
            # 在這裡代為開倉會讓帳上多出策略沒下過的部位

        self.account.remove_closed_positions()

        return close_records
