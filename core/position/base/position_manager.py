import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union

from loguru import logger

from core.models import BaseAccount, BaseOrder, BasePosition, BaseTradeRecord
from core.portfolio import order_rules
from core.utils import Action, PositionType, TimeUtils

"""BasePositionManager: 市場與商品皆無關的部位管理骨架（開倉樣板、FIFO 拆單與方向篩選）"""


class BasePositionManager(ABC):
    """
    部位管理層的抽象基底

    開倉前的判準（方向與動作一致、同標的不得雙向持倉）、FIFO 拆單、方向篩選與
    「平倉量不足時只平已有部位」的處理都與市場與商品皆無關，故收在此；
    單筆部位的記帳（成本攤提、保證金、損益公式）由各市場實作 `_book_open()` 與
    `close_single_position()`。
    """

    def __init__(
        self, account: BaseAccount, event_counts: Optional[Dict[str, int]] = None
    ) -> None:
        self.account: BaseAccount = account

        # 與引擎共用同一個 dict，拒單計數才會反映到報表（傳 None 時自行持有，供單獨測試）
        self.event_counts: Dict[str, int] = (
            event_counts if event_counts is not None else {}
        )

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """部位管理層的設定掛點；由各市場實作"""

        pass

    def count_event(self, key: str) -> None:
        """事件計數加一；單獨建模型時 dict 可能沒有這個 key，用 `.get` 補 0"""

        self.event_counts[key] = self.event_counts.get(key, 0) + 1

    @staticmethod
    def calculate_holding_days(
        entry_date: Union[datetime.date, datetime.datetime, None],
        exit_date: Union[datetime.date, datetime.datetime, None],
    ) -> int:
        """計算持有曆日數（非交易日），同日開平倉為 0 天；Tick 級別的 datetime 只取日期部分"""

        entry: Optional[datetime.date] = TimeUtils.to_date(entry_date)
        exit_: Optional[datetime.date] = TimeUtils.to_date(exit_date)
        if entry is None or exit_ is None:
            return 0

        return (exit_ - entry).days

    def open_position(self, order: BaseOrder) -> Optional[BasePosition]:
        """
        - Description:
            開倉：方向一致性 → 同標的不得雙向持倉 → 各市場入帳 → 累計滑價

            **判準只有這一份**：兩個市場原本各寫一套，台股方向不一致時靜默回 None、
            期貨有 warning；反向持倉檢查台股分散在多空兩處。判準相同、寫法不同，
            改一邊另一邊不會跟著改，也不會有測試失敗。

            **同一標的不允許雙向持倉，多空兩端都要檢查**：只擋單邊的話，反序開倉
            就會讓同一檔同時掛著多空兩個部位，兩邊各自盯市、各自計算維持率，
            帳面曝險與實際完全對不上。期貨的多空並存同樣會押著兩份保證金，
            而交易所對沖部位只收單邊，可開口數因此被系統性低估。
        - Parameters:
            - order: BaseOrder
                開倉訂單（多單買進、空單賣出）
        - Return:
            - Optional[BasePosition]
                開倉成功的部位；被判準或餘額擋下時為 None
        """

        if not order_rules.is_opening(order):
            logger.warning(
                f"[Open Position] 方向與動作不一致：{order.symbol} "
                f"{order.position_type} / {order.action}，拒絕開倉"
            )
            return None

        opposite_type: PositionType = (
            PositionType.SHORT
            if order.position_type == PositionType.LONG
            else PositionType.LONG
        )
        if self.account.check_has_position(order.symbol, opposite_type):
            logger.warning(
                f"[Open Position] {order.symbol} 已有 {opposite_type.value} 部位，"
                f"不允許同標的雙向持倉，拒絕開倉"
            )
            return None

        position: Optional[BasePosition] = self._book_open(order)

        # **開成了才算滑價**：被拒的單沒有成交，價差也就不存在
        if position is not None:
            self.accrue_slippage_cost(
                order, order.volume, self.get_position_unit_size(position)
            )

        return position

    @abstractmethod
    def _book_open(self, order: BaseOrder) -> Optional[BasePosition]:
        """
        - Description:
            開倉入帳：算開倉成本、檢查餘額（或保證金）、建部位並扣款

            進到這裡的單已通過方向與雙向持倉的判準；餘額不足要自行 warning 並
            `count_event("rejected_insufficient_balance")`，只回 None 的話引擎會
            靜默跳過，回測結果看起來只是少一筆交易。
        - Parameters:
            - order: BaseOrder
                已通過判準的開倉訂單
        - Return:
            - Optional[BasePosition]
                入帳的部位；餘額不足時為 None
        """

        pass

    @abstractmethod
    def get_position_unit_size(self, position: BasePosition) -> int:
        """一單位的計價單位數：台股一張 1,000 股、期貨為該契約的乘數（供滑價統計）"""

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

    def close_specific_position(
        self, position: BasePosition, order: BaseOrder
    ) -> List[BaseTradeRecord]:
        """
        - Description:
            平掉**指定的這一筆**部位（數量以該筆部位為上限），不走同標的 FIFO

            引擎強制出場的判準有的是部位層級：停券回補只對 `MARGIN` 的融券生效、持有天數
            逐筆起算。同標的先借券（SBL）再融券時，FIFO 會平到不該平的 SBL 那一筆，
            MARGIN 的空單反而留在場上，而且不會有任何錯誤訊息。策略下的平倉單仍走
            `close_position()` 的 FIFO。
        - Parameters:
            - position: BasePosition
                要平的部位
            - order: BaseOrder
                平倉訂單（方向、價格、數量）；數量超過部位時只平到該部位的量
        - Return:
            - List[BaseTradeRecord]
                平倉產生的交易紀錄；部位已平或沒有量可平時為空
        - Raise:
            - ValueError
                訂單的標的或方向與部位不符（組單的程式有錯，不可靜默吞掉）
        """

        if (
            order.symbol != position.symbol
            or self.resolve_target_position_type(order) != position.position_type
        ):
            raise ValueError(
                f"[Close Position] 平倉單 {order.symbol} {order.action} 與部位 "
                f"{position.symbol} {position.position_type} 不符"
            )

        close_volume: int = min(position.volume, order.volume)
        if position.is_closed or close_volume <= 0:
            return []

        record: Optional[BaseTradeRecord] = self.close_single_position(
            position, order, close_volume
        )
        self.account.remove_closed_positions()
        return [record] if record is not None else []
