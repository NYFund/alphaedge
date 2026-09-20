import datetime
import itertools
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.models import BrokerPositionSnapshot, ExecutionReport
from core.utils import PositionType

"""
部位歸屬帳：券商合併部位拆不回策略，只能本地自己記

**所有多策略的難題都來自同一件事：券商端只有一本合併帳。** 一個帳戶、一組部位、
一筆餘額，券商完全不知道「策略」是什麼。策略是純本地概念，所以要做的就是維護
一本本地歸屬帳，並在它與券商合併帳之間保持隨時可對帳。

不變式：**Σ 各策略 lot 淨額（依 symbol、direction）＝ 券商部位**。

平倉**只沖銷該策略自己的 lot**，順序與回測的 `BasePositionManager.close_position()`
一致（FIFO：最早開倉者先平）。兩邊順序不一致的話，已實現損益會對不上，
而券商端的合計還是對的——對帳看不出來。
"""

# 未歸屬部位的保留策略名。第一次啟動、lot 表遺失、券商強制平倉都會落到這裡
UNATTRIBUTED_STRATEGY: str = "__unattributed__"


class PositionAttributionLedger:
    """
    - Description:
        策略層部位歸屬帳

        帳戶層的合計由各策略帳加總得出，**不另外維護一份可能漂移的副本**。
    """

    def __init__(
        self,
        dao: LiveTradeDAO,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立歸屬帳
        - Parameters:
            - dao: LiveTradeDAO
                實盤紀錄庫
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間（台北時區 aware）
        """

        self.dao: LiveTradeDAO = dao
        self._now: Callable[[], datetime.datetime] = now_provider
        self._lot_sequence: itertools.count = itertools.count(1)

    # === 開倉 ===
    def open_lot(
        self,
        strategy_name: str,
        fill: ExecutionReport,
        direction: PositionType,
        client_order_id: Optional[str] = None,
    ) -> str:
        """
        - Description:
            開倉成交時新增一筆 lot
        - Parameters:
            - strategy_name: str
                歸屬策略
            - fill: ExecutionReport
                成交回報
            - direction: PositionType
                多空方向
            - client_order_id: Optional[str]
                來源委託
        - Return:
            - str
                新建的 `lot_id`
        """

        moment: datetime.datetime = fill.ts or self._now()
        lot_id: str = self._next_lot_id(moment)

        self.dao.open_lot(
            {
                "lot_id": lot_id,
                "strategy_name": strategy_name,
                "symbol": fill.symbol,
                "direction": direction.value,
                "volume": fill.volume,
                "open_date": moment.date(),
                "open_price": fill.price,
                "client_order_id": client_order_id,
            }
        )
        self.dao.conn.commit()
        return lot_id

    def adopt_broker_positions(
        self, positions: List[BrokerPositionSnapshot]
    ) -> List[str]:
        """
        - Description:
            把券商端有、但歸屬帳沒有的部位收進 `__unattributed__`

            會走到這裡的情況：第一次啟動、lot 表遺失、券商因追繳或違約強制平倉
            造成的差額。**這些部位只允許平倉**，不參與任何策略的新倉判斷。

            **刻意不平均分配給各策略**：券商給的是合併部位，分不回策略；
            猜一個分法會讓兩支策略的已實現損益都是錯的，而合計仍然正確。
        - Parameters:
            - positions: List[BrokerPositionSnapshot]
                券商端部位
        - Return:
            - List[str]
                新建的 `lot_id` 清單
        """

        local: Dict[Tuple[str, str], int] = self.get_account_positions()
        created: List[str] = []

        for position in positions:
            key: Tuple[str, str] = (position.symbol, position.direction.value)
            gap: int = position.volume - local.get(key, 0)
            if gap <= 0:
                continue

            moment: datetime.datetime = self._now()
            lot_id: str = self._next_lot_id(moment)
            self.dao.open_lot(
                {
                    "lot_id": lot_id,
                    "strategy_name": UNATTRIBUTED_STRATEGY,
                    "symbol": position.symbol,
                    "direction": position.direction.value,
                    "volume": gap,
                    "open_date": moment.date(),
                    "open_price": position.avg_price,
                    "client_order_id": None,
                }
            )
            created.append(lot_id)
            logger.warning(
                f"券商部位 {position.symbol} {position.direction.value} {gap} "
                f"在歸屬帳中查無來源，已收進 {UNATTRIBUTED_STRATEGY}（只允許平倉）"
            )

        self.dao.conn.commit()
        return created

    # === 平倉 ===
    def close_lots(
        self, strategy_name: str, symbol: str, volume: int, direction: PositionType
    ) -> List[Tuple[str, int]]:
        """
        - Description:
            沖銷 lot；**只動該策略自己的**，順序 FIFO

            順序與回測的 `BasePositionManager.close_position()` 一致（最早開倉者先平）。
            兩邊不一致的話，已實現損益會對不上，而券商端的合計還是對的——
            對帳看不出來。

            **可沖銷量不足時只沖銷已有的並記 warning**，不拋出：實盤走到這裡
            代表歸屬帳與實際成交已經對不上，但那張平倉單已經成交了，
            拋出只會讓後面的成交也處理不了。差額由對帳在下一個段落抓出來。
        - Parameters:
            - strategy_name: str
                歸屬策略
            - symbol: str
                商品代號
            - volume: int
                要沖銷的數量
            - direction: PositionType
                被平掉的部位方向
        - Return:
            - List[Tuple[str, int]]
                `[(lot_id, 本次沖銷量)]`
        """

        lots: List[Dict[str, object]] = [
            lot
            for lot in self.dao.get_open_lots(strategy_name, symbol)
            if lot["direction"] == direction.value
        ]

        remaining: int = volume
        closed: List[Tuple[str, int]] = []
        moment: datetime.datetime = self._now()

        for lot in lots:
            if remaining <= 0:
                break

            lot_id: str = str(lot["lot_id"])
            available: int = int(lot["volume"])
            taken: int = min(available, remaining)

            if taken >= available:
                self.dao.close_lot(lot_id, moment)
            else:
                self.dao.reduce_lot(lot_id, taken)

            closed.append((lot_id, taken))
            remaining -= taken

        if remaining > 0:
            logger.warning(
                f"[Attribution] {strategy_name} 的 {symbol} 可沖銷量不足，"
                f"要求 {volume}、實際只沖銷 {volume - remaining}；"
                "差額將由下一次對帳抓出"
            )

        self.dao.conn.commit()
        return closed

    # === 查詢 ===
    def get_strategy_positions(self, strategy_name: str) -> Dict[Tuple[str, str], int]:
        """
        - Description:
            某支策略的未平倉部位
        - Parameters:
            - strategy_name: str
                策略名
        - Return:
            - Dict[Tuple[str, str], int]
                `{(symbol, direction): 淨額}`
        """

        return self._aggregate(self.dao.get_open_lots(strategy_name=strategy_name))

    def get_account_positions(self) -> Dict[Tuple[str, str], int]:
        """
        - Description:
            帳戶層合計：各策略帳的加總

            **不另外維護一份副本**：兩份紀錄必然漂移，而漂移的那一刻兩邊都看起來正確。
        - Return:
            - Dict[Tuple[str, str], int]
                `{(symbol, direction): 淨額}`
        """

        return self._aggregate(self.dao.get_open_lots())

    def get_holder(self, symbol: str) -> Optional[str]:
        """
        - Description:
            這個標的目前被哪一支策略持有（跨策略守門用）

            **未歸屬部位要回傳 `__unattributed__`，不可回傳 None。**
            回 None 等於放行——策略會對一檔券商端已有部位的標的開新倉，
            踩進的正是守門要防的三個坑：券商端反向沖銷、台股同日一買一賣
            被判成當沖、以及歸屬帳一對多的拆分。
        - Parameters:
            - symbol: str
                商品代號
        - Return:
            - Optional[str]
                策略名；**無人持有時才是 None**
        """

        return self.dao.get_symbol_holder(symbol)

    def get_strategy_names(self) -> List[str]:
        """目前持有部位的策略清單（含 `__unattributed__`）"""

        return sorted({str(lot["strategy_name"]) for lot in self.dao.get_open_lots()})

    # === 對帳 ===
    def diff_against_broker(
        self, positions: List[BrokerPositionSnapshot]
    ) -> Dict[Tuple[str, str], Tuple[int, int]]:
        """
        - Description:
            比對「Σ 各策略 lot 淨額」與券商部位

            這是多策略的對帳式。差異**無法歸因到單支策略**，所以只回傳差異本身，
            由呼叫端走帳戶層降級——猜是哪一支的代價是讓真正有問題的那支繼續交易。
        - Parameters:
            - positions: List[BrokerPositionSnapshot]
                券商端部位
        - Return:
            - Dict[Tuple[str, str], Tuple[int, int]]
                `{(symbol, direction): (本地淨額, 券商淨額)}`，只含不一致者
        """

        local: Dict[Tuple[str, str], int] = self.get_account_positions()
        broker: Dict[Tuple[str, str], int] = {
            (position.symbol, position.direction.value): position.volume
            for position in positions
        }

        differences: Dict[Tuple[str, str], Tuple[int, int]] = {}
        for key in set(local) | set(broker):
            local_volume: int = local.get(key, 0)
            broker_volume: int = broker.get(key, 0)
            if local_volume != broker_volume:
                differences[key] = (local_volume, broker_volume)
        return differences

    # === 內部 ===
    @staticmethod
    def _aggregate(lots: List[Dict[str, object]]) -> Dict[Tuple[str, str], int]:
        """把 lot 清單彙總成 `{(symbol, direction): 淨額}`"""

        totals: Dict[Tuple[str, str], int] = {}
        for lot in lots:
            key: Tuple[str, str] = (str(lot["symbol"]), str(lot["direction"]))
            totals[key] = totals.get(key, 0) + int(lot["volume"])
        return totals

    def _next_lot_id(self, moment: datetime.datetime) -> str:
        """
        產生 lot id

        **前綴是時戳、後綴是遞增序號**：`get_open_lots()` 以 `(開倉日, lot_id)` 排序，
        而 FIFO 的正確性建立在「同一天內 lot_id 遞增 ＝ 開倉先後」上。
        純隨機碼會讓同一天的沖銷順序變成不可預期，兩次重跑得到不同的已實現損益。
        """

        return f"{moment.strftime('%Y%m%d%H%M%S')}-{next(self._lot_sequence):06d}"
