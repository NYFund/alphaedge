import datetime
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.attribution.position_ledger import PositionAttributionLedger
from core.managers.base.position_manager import BasePositionManager
from core.models import BrokerPositionSnapshot, StockPositionSnapshot

"""
Reconciler：本地與券商不一致時，要在下一張委託之前發現

多策略的對帳式是 **Σ 各策略 lot 淨額 ＝ 券商部位**。券商端只有合併部位，因此
差異**無法歸因到單支策略**——不要嘗試猜，猜錯的代價是讓真正有問題的那支繼續交易。
一律走帳戶層降級，全體轉 `REDUCE_ONLY`。

另外比對一條**內部一致性**：各策略 `Account` 的部位合計是否等於 lot 表的淨額。
這條不一致代表本地自己的兩份紀錄就對不上（回報處理有 bug），與券商無關，
但同樣要擋住新倉。

**不自動修正本地部位。** 自動修正會把真正的 bug（例如回報漏接）蓋掉，
而被蓋掉的 bug 明天還會再發生一次。要修正得由人確認後以旗標重建。
"""


@dataclass
class ReconcileResult:
    """一次對帳的結果"""

    # `{(symbol, direction): (本地淨額, 券商淨額)}`
    broker_differences: Dict[Tuple[str, str], Tuple[int, int]] = field(
        default_factory=dict
    )
    # `{(symbol, direction): (Account 合計, lot 表淨額)}`
    internal_differences: Dict[Tuple[str, str], Tuple[int, int]] = field(
        default_factory=dict
    )
    # `{symbol: (本地券別, 券商券別)}`；只有股票有
    order_cond_differences: Dict[str, Tuple[Optional[str], Optional[str]]] = field(
        default_factory=dict
    )

    @property
    def is_consistent(self) -> bool:
        """三項都沒有差異才算一致"""

        return not (
            self.broker_differences
            or self.internal_differences
            or self.order_cond_differences
        )

    def describe(self) -> str:
        """單行摘要，供 log 與推播使用"""

        parts: List[str] = []
        if self.broker_differences:
            parts.append(f"與券商 {len(self.broker_differences)} 筆不一致")
        if self.internal_differences:
            parts.append(f"本地兩份紀錄 {len(self.internal_differences)} 筆不一致")
        if self.order_cond_differences:
            parts.append(f"融資券別 {len(self.order_cond_differences)} 筆不一致")
        return "；".join(parts) or "一致"


class Reconciler:
    """
    - Description:
        對帳器

        時點：啟動時、每個段落開始前、盤後。

        **自己不改交易模式**，只送降級事件給風控——散在各元件各自切換的話，
        沒有任何一處知道「現在到底能不能送單」。
    """

    def __init__(
        self,
        ledger: PositionAttributionLedger,
        position_managers: Dict[str, BasePositionManager],
        dao: LiveTradeDAO,
        run_id: str = "",
        on_degrade: Optional[Callable[[str], None]] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立對帳器
        - Parameters:
            - ledger: PositionAttributionLedger
                部位歸屬帳
            - position_managers: Dict[str, BasePositionManager]
                各策略的部位管理器（取其 `Account` 做內部一致性比對）
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_id: str
                本次啟動的識別碼
            - on_degrade: Optional[Callable[[str], None]]
                降級回呼
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.ledger: PositionAttributionLedger = ledger
        self.position_managers: Dict[str, BasePositionManager] = position_managers
        self.dao: LiveTradeDAO = dao
        self.run_id: str = run_id
        self._on_degrade: Optional[Callable[[str], None]] = on_degrade
        self._now: Callable[[], datetime.datetime] = now_provider

    def check(self, positions: List[BrokerPositionSnapshot]) -> ReconcileResult:
        """
        - Description:
            執行一次對帳；不一致時寫事件與快照並送降級事件
        - Parameters:
            - positions: List[BrokerPositionSnapshot]
                券商端部位
        - Return:
            - ReconcileResult
                對帳結果
        """

        result: ReconcileResult = ReconcileResult(
            broker_differences=self.ledger.diff_against_broker(positions),
            internal_differences=self._check_internal_consistency(),
            order_cond_differences=self._check_order_cond(positions),
        )

        self._write_snapshots(positions)

        if result.is_consistent:
            logger.info("對帳一致")
            return result

        message: str = f"對帳不一致：{result.describe()}"
        logger.error(message)
        self._write_event(message, result)

        # **差異無法歸因到單支策略，故走帳戶層**：猜錯的代價是讓真正有問題的那支繼續交易
        self._degrade(message)
        return result

    def _check_internal_consistency(self) -> Dict[Tuple[str, str], Tuple[int, int]]:
        """
        比對各策略 `Account` 的部位合計與 lot 表淨額

        這條不一致代表本地自己的兩份紀錄就對不上（回報處理有 bug），與券商無關，
        但同樣要擋住新倉——兩份都不可信的時候，再送單只會讓事情更複雜。

        **只比對「本次有載入 `position_manager` 的策略」**，兩側取同一組名單：

        - `__unattributed__` **依設計只存在於 lot 帳本**。它不是策略、沒有
          `Account`，另一側本來就不會有對應的列。把它算進差異的話，只要帳上有
          接管部位，每個段落的對帳都會不一致、每天都降級成 `REDUCE_ONLY`，
          而真正的回報處理 bug 會淹沒在這些雜訊裡。
        - **本次沒載入的策略同樣被排除**：它的 `Account` 根本沒被建出來，
          拿空的一側去比必然差，那不是不一致而是沒有資料。代價是那些策略的
          lot 這一輪不受本檢查保護——`diff_against_broker()` 仍涵蓋它們，
          因為那一側比的是券商總量。
        """

        from_accounts: Dict[Tuple[str, str], int] = {}
        for manager in self.position_managers.values():
            for position in manager.account.positions:
                if position.is_closed:
                    continue
                key: Tuple[str, str] = (
                    position.symbol,
                    position.position_type.value,
                )
                from_accounts[key] = from_accounts.get(key, 0) + position.volume

        from_lots: Dict[Tuple[str, str], int] = {}
        for name in self.position_managers:
            for key, volume in self.ledger.get_strategy_positions(name).items():
                from_lots[key] = from_lots.get(key, 0) + volume

        differences: Dict[Tuple[str, str], Tuple[int, int]] = {}
        for key in set(from_accounts) | set(from_lots):
            account_volume: int = from_accounts.get(key, 0)
            lot_volume: int = from_lots.get(key, 0)
            if account_volume != lot_volume:
                differences[key] = (account_volume, lot_volume)
        return differences

    def _check_order_cond(
        self, positions: List[BrokerPositionSnapshot]
    ) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
        """
        股票另外比對融資券別

        同一檔的現股多單與融券空單在券商端是兩筆不同的部位。只比
        「代號 ＋ 方向 ＋ 數量」的話，兩者互換時數字會剛好對得上，對帳就看不出差異。

        本地目前不記券別（`live_position_lot` 沒有這一欄），故這裡只在券商回報
        同一標的出現**多種券別**時示警——那代表歸屬帳的一對一假設已經破了。
        """

        by_symbol: Dict[str, set] = {}
        for position in positions:
            if not isinstance(position, StockPositionSnapshot):
                continue
            cond: Optional[str] = (
                position.order_cond.value if position.order_cond else None
            )
            by_symbol.setdefault(position.symbol, set()).add(cond)

        return {
            symbol: (None, "、".join(sorted(str(c) for c in conds)))
            for symbol, conds in by_symbol.items()
            if len(conds) > 1
        }

    def _write_snapshots(self, positions: List[BrokerPositionSnapshot]) -> None:
        """
        寫入兩邊的部位快照

        **一致時也要寫**：出事後回頭查「昨天是不是就已經差了」，靠的就是這些快照。
        只在不一致時寫的話，那條時間軸會是斷的。
        """

        today: datetime.date = self._now().date()

        for (symbol, direction), volume in self.ledger.get_account_positions().items():
            self.dao.upsert_position_snapshot(
                {
                    "date": today,
                    "strategy_name": "__account__",
                    "symbol": symbol,
                    "source": "account",
                    "direction": direction,
                    "volume": volume,
                }
            )

        for name in self.ledger.get_strategy_names():
            for (symbol, direction), volume in self.ledger.get_strategy_positions(
                name
            ).items():
                self.dao.upsert_position_snapshot(
                    {
                        "date": today,
                        "strategy_name": name,
                        "symbol": symbol,
                        "source": "local",
                        "direction": direction,
                        "volume": volume,
                    }
                )

        for position in positions:
            self.dao.upsert_position_snapshot(
                {
                    "date": today,
                    "strategy_name": "__broker__",
                    "symbol": position.symbol,
                    "source": "broker",
                    "direction": position.direction.value,
                    "volume": position.volume,
                    "avg_price": position.avg_price,
                    "unrealized_pnl": position.unrealized_pnl,
                }
            )

        self.dao.conn.commit()

    def _write_event(self, message: str, result: ReconcileResult) -> None:
        """寫一筆風控事件，明細帶上兩邊的數字"""

        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": None,
                "severity": "CRITICAL",
                "category": "RECONCILE_MISMATCH",
                "message": message,
                "detail_json": str(
                    {
                        "broker": result.broker_differences,
                        "internal": result.internal_differences,
                        "order_cond": result.order_cond_differences,
                    }
                ),
                "occurred_at": self._now(),
            }
        )

    def _degrade(self, reason: str) -> None:
        """送降級事件給風控；**對帳器自己不改模式**"""

        if self._on_degrade is not None:
            self._on_degrade(reason)
