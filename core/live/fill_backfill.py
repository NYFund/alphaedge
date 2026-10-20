import datetime
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.notify.base import BaseNotifier, NotifyLevel, notify_safely
from core.live.oms.order_manager import OrderManager
from core.live.risk.event_log import RiskEventLogger
from core.models import ExecutionReport
from core.portfolio import order_rules
from core.utils import Action, PositionType

"""
以 `live_fill` 的成交事實回填委託與歸屬帳

股票線與期貨線是兩個行程、同一個帳號，券商的成交回報兩個行程都收得到，而 `live_fill`
只有一張。成交已經在表裡、擁有者的委託卻沒有套進去，發生在三種時候：回報先到非擁有者
那邊而擁有者行程已經收線；擁有者行程送單後、收回報前死掉；送單逾時被標失敗、成交
接著才到。盤中的認領（`OrderManager._claim_fill()`）只救得了第一種的一半，其餘只剩
這裡能補。

**兩個時點都要跑**：盤後（殘量處理之前，否則已成交的單會被標成日終未成交再排補平）
與段落啟動時（接管委託之後、以歸屬帳重建部位之前——順序反了，這些成交會被重建收進
`__unattributed__`，對帳一致、策略卻失去那口部位）。
"""


class FillBackfiller:
    """
    - Description:
        把 `live_fill` 裡多於委託已記成交量的部分，補套進委託並記進歸屬帳
    """

    def __init__(
        self,
        order_manager: OrderManager,
        account_sync: Optional[AccountSynchronizer],
        dao: LiveTradeDAO,
        run_id: str,
        now_provider: Callable[[], datetime.datetime] = now_live,
        notifier: Optional[BaseNotifier] = None,
    ) -> None:
        """
        - Description:
            建立回填器
        - Parameters:
            - order_manager: OrderManager
                本行程的委託管理（只回填它負責的策略）
            - account_sync: Optional[AccountSynchronizer]
                帳戶同步器；None 時只回填委託、不動歸屬帳
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_id: str
                本次執行識別碼（事件歸屬）
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - notifier: Optional[BaseNotifier]
                推播管道
        """

        self.order_manager: OrderManager = order_manager
        self.account_sync: Optional[AccountSynchronizer] = account_sync
        self.dao: LiveTradeDAO = dao
        self.notifier: Optional[BaseNotifier] = notifier
        self.events: RiskEventLogger = RiskEventLogger(
            dao, run_id, now_provider=now_provider
        )

    def backfill(self, run_date: datetime.date) -> int:
        """
        - Description:
            對本行程每張當日委託：把尚未歸屬的同序號成交歸給它，成交合計多於委託
            已記的量就補套進委託、記進歸屬帳，並以 CRITICAL 事件留下紀錄——
            它代表有一筆成交沒有被擁有者即時處理。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                被回填的委託數
        """

        backfilled: int = 0
        for row in self.order_manager.orders_of_day(run_date):
            broker_seqno: str = str(row.get("broker_seqno") or "")
            if not broker_seqno:
                continue
            client_order_id: str = str(row["client_order_id"])
            strategy_name: str = str(row["strategy_name"])
            # 序號每天從頭配發：只認這張委託那一天的成交，跨日同序號是別張單的
            trade_date: datetime.date = (
                datetime.date.fromisoformat(str(row["trade_date"])[:10])
                if row.get("trade_date")
                else run_date
            )

            fills: List[Dict[str, Any]] = self.dao.get_fills_by_seqno(
                broker_seqno, trade_date
            )
            for fill in fills:
                if not fill.get("client_order_id") and self.dao.attribute_fill(
                    trade_date,
                    broker_seqno,
                    str(fill["broker_trade_id"]),
                    client_order_id,
                    strategy_name,
                ):
                    fill["client_order_id"] = client_order_id
            mine: List[Dict[str, Any]] = [
                fill for fill in fills if fill.get("client_order_id") == client_order_id
            ]

            recorded: int = int(row.get("filled_volume") or 0)
            total: int = sum(int(fill.get("volume") or 0) for fill in mine)
            if total <= recorded:
                continue

            reports: List[ExecutionReport] = reports_after(mine, recorded)
            self.order_manager.book_backfilled_fills(row, reports)
            if self.account_sync is not None:
                position_type: PositionType = PositionType(str(row["position_type"]))
                closing: bool = order_rules.is_closing_record(
                    str(row["position_type"]), str(row["action"])
                )
                for report in reports:
                    self.account_sync.book_backfilled_fill(
                        report, strategy_name, position_type, closing
                    )

            message: str = (
                f"委託 {client_order_id}（{row['symbol']}）本地只記到成交 {recorded}，"
                f"成交明細合計 {total}；成交回報當時沒有套進本行程，"
                f"依成交明細回填 {total - recorded}，歸屬帳已更新"
            )
            logger.error(message)
            self.events.write(
                category="FILL_BACKFILLED",
                severity=NotifyLevel.CRITICAL,
                message=message,
                strategy_name=strategy_name,
                symbol=str(row["symbol"]),
                client_order_id=client_order_id,
            )
            notify_safely(self.notifier, NotifyLevel.CRITICAL, "成交回報回填", message)
            backfilled += 1

        if backfilled:
            logger.warning(f"依成交明細回填 {backfilled} 張委託的成交")
        return backfilled


def reports_after(fills: List[Dict[str, Any]], recorded: int) -> List[ExecutionReport]:
    """
    成交列 → 尚未套進委託的那部分成交回報

    成交依時間排序，前面累計到 `recorded` 的量視為委託已經套過（委託只會依序累進）；
    跨在邊界上的那一筆只取超出的部分。
    """

    reports: List[ExecutionReport] = []
    skipped: int = 0
    for fill in fills:
        volume: int = int(fill.get("volume") or 0)
        if skipped + volume <= recorded:
            skipped += volume
            continue
        remaining: int = volume - max(recorded - skipped, 0)
        skipped = recorded
        reports.append(
            ExecutionReport(
                broker_seqno=str(fill.get("broker_seqno") or ""),
                broker_trade_id=str(fill.get("broker_trade_id") or ""),
                symbol=str(fill.get("symbol") or ""),
                action=Action(str(fill.get("action"))),
                price=float(fill.get("price") or 0.0),
                volume=remaining,
                ts=_parse_time(fill.get("filled_at")),
                raw={"source": "live_fill"},
            )
        )
    return reports


def _parse_time(value: Any) -> Optional[datetime.datetime]:
    """紀錄庫的 ISO 時間字串 → datetime；空值回 None"""

    if isinstance(value, datetime.datetime):
        return value
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value))
