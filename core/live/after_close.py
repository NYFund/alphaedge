import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution import order_preprocess
from core.live.account_sync import AccountSynchronizer
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.notify.base import BaseNotifier, notify_safely
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.report.live_reporter import LiveReporter
from core.live.report.parity_checker import ParityChecker, ParityDiff
from core.live.risk.trading_mode import TradingModeState
from core.utils import PositionType

"""
盤後作業：把當天發生的事收攏成可稽核的結果

**與送單段落沒有共用任何狀態**，所以獨立成一個類別而不是留在 `LiveTrader` 裡：
它自己 `connect()` / `finally: close()`，不碰資金保留、段落時窗與跨策略守門，
`run.py` 也是以 `--phase after_close` 走完全獨立的分支。

**只負責「寫下明天要補的事」，不負責執行它**：`apply_pending_actions()` 留在
`LiveTrader`，因為待辦是在**開盤段**被補平的。寫入端與執行端本來就不對稱，
硬抽成一個服務會讓兩邊都得繞一層。
"""


class AfterCloseRunner:
    """
    - Description:
        盤後作業的執行者

        **收資料源而不是整組 `StrategyContext`**：盤後只需要在結束時關掉連線，
        收整組 context 會讓本模組相依 `trader.py`，而 `trader.py` 又要 import
        本模組——那是一個檔案層級的循環。
    """

    def __init__(
        self,
        data_feeds: Sequence[BaseLiveDataFeed],
        broker: Any,
        order_manager: OrderManager,
        account_sync: AccountSynchronizer,
        reconciler: Reconciler,
        reporter: LiveReporter,
        mode_state: TradingModeState,
        dao: LiveTradeDAO,
        run_id: str,
        notifier: Optional[BaseNotifier] = None,
        now_provider: Callable[[], datetime.datetime] = now_live,
        parity_checker: Optional[ParityChecker] = None,
    ) -> None:
        """
        - Description:
            建立盤後作業執行者
        - Parameters:
            - data_feeds: Sequence[BaseLiveDataFeed]
                各策略的資料源；只用在結束時關閉連線
            - broker: Any
                券商閘道
            - order_manager: OrderManager
                委託管理
            - account_sync: AccountSynchronizer
                帳戶同步
            - reconciler: Reconciler
                對帳器
            - reporter: LiveReporter
                日報輸出
            - mode_state: TradingModeState
                交易模式狀態機
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_id: str
                本次執行識別碼
            - notifier: Optional[BaseNotifier]
                推播管道
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
            - parity_checker: Optional[ParityChecker]
                訊號 parity 比對器；None 時跳過比對並記 warning
        """

        self.data_feeds: Sequence[BaseLiveDataFeed] = data_feeds
        self.broker: Any = broker
        self.order_manager: OrderManager = order_manager
        self.account_sync: AccountSynchronizer = account_sync
        self.reconciler: Reconciler = reconciler
        self.reporter: LiveReporter = reporter
        self.mode_state: TradingModeState = mode_state
        self.dao: LiveTradeDAO = dao
        self.run_id: str = run_id
        self.notifier: Optional[BaseNotifier] = notifier
        self.parity_checker: Optional[ParityChecker] = parity_checker
        self._now: Callable[[], datetime.datetime] = now_provider

        # 本次對帳結果；`run.py` 由它決定退出碼，故盤後跑完要回填給 `LiveTrader`
        self.last_reconcile: Optional[Any] = None

    def run(self) -> Dict[str, Any]:
        """
        - Description:
            盤後作業：刷新委託、對帳、回填成本、殘量處理、輸出報表

            **和送單段落分開跑**（`--phase after_close`）：它不送任何新倉單，
            只把當天發生的事收攏成可稽核的結果，並把「明天要補的事」寫下來。
        - Return:
            - Dict[str, Any]
                本次盤後作業的摘要（報表路徑、殘量筆數、滑價統計）
        """

        today: datetime.date = self._now().date()
        logger.info(f"=== 盤後作業開始（{today}）===")

        try:
            self.broker.connect()
            self.mode_state.load()

            # 1. 刷新委託狀態：ROD 未成交單在券商端日終自動失效
            self.expire_open_orders()

            # 2. 對帳與快照
            positions: List[Any] = self.broker.get_positions()
            self.account_sync.rebuild_from_broker(positions)
            self.last_reconcile = self.reconciler.check(positions)

            # 3. 回填券商實際費用（估算值保留，差額是校正成本設定的依據）
            self.backfill_actual_costs(today)

            # 4. 未成交殘量依政策處理
            remainders: int = self.handle_unfilled_remainders(today)

            # 5. 報表與滑價
            reports: Dict[str, Path] = self.reporter.write_daily_reports(today)
            slippage: Dict[str, float] = self.reporter.summarize_slippage(today)

            # 6. 訊號 parity：同一支策略在回測與實盤有沒有送出同一批委託。
            # **排在報表之後**：比對要讀當日委託，而那些在前面幾步已經寫完了
            unexplained: int = self.check_signal_parity(today)

            return {
                "reports": reports,
                "pending_actions": remainders,
                "slippage": slippage,
                "unexplained_parity": unexplained,
            }
        finally:
            self.broker.close()
            for feed in self.data_feeds:
                feed.close()
            logger.info("=== 盤後作業結束 ===")

    def check_signal_parity(self, run_date: datetime.date) -> int:
        """
        - Description:
            比對當日實盤委託與同一天回測會送出的委託

            **未解釋的差異一律推播 CRITICAL**：訊號漂移不會有任何錯誤訊息，
            它只會讓回測績效靜靜失去參考價值。已知的制度性差異（快照口徑、
            跨策略守門、資金排擠）各有類別，不佔用 `UNEXPLAINED`。

            **比對失敗不可中斷盤後作業**：報表與殘量處理都已經完成了，
            為了一個比對把它們的結束流程拖掉並不划算。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                未解釋差異的筆數；比對未執行時為 0
        """

        if self.parity_checker is None:
            logger.warning("未注入 parity 比對器，本日不做訊號一致性比對")
            return 0

        try:
            result: Dict[str, List[ParityDiff]] = self.parity_checker.check(run_date)
        except Exception as exc:
            logger.opt(exception=True).error(f"訊號 parity 比對失敗：{exc}")
            self._notify("CRITICAL", "訊號 parity 比對失敗", str(exc))
            return 0

        unexplained: Dict[str, int] = {
            name: sum(1 for diff in diffs if diff.is_unexplained)
            for name, diffs in result.items()
        }
        total: int = sum(unexplained.values())
        if total == 0:
            logger.info("訊號 parity 比對通過：無未解釋的差異")
            return 0

        detail: str = "、".join(
            f"{name} {count} 筆" for name, count in unexplained.items() if count
        )
        self._notify(
            "CRITICAL",
            "訊號 parity 有未解釋的差異",
            f"{run_date} 共 {total} 筆（{detail}），列為隔日第一優先",
        )
        return total

    def expire_open_orders(self) -> List[Any]:
        """
        - Description:
            把當日仍未終結的委託標成已撤

            ROD 單在券商端日終自動失效，**本地要跟著標**：不標的話，
            明天的恢復流程會把它們當成「還在場上」去接管，然後撤一張不存在的單。
        - Return:
            - List[Any]
                被標記的委託
        """

        self.order_manager.refresh_from_broker()
        return self.order_manager.expire_unfinished(self._now().date())

    def backfill_actual_costs(self, run_date: datetime.date) -> int:
        """
        - Description:
            以券商的損益明細回填當日成交的實際手續費與稅

            **估算值不覆蓋**：兩者分欄保存，差額才是校正成本設定的依據；
            併成一欄之後就再也算不出「估得準不準」。

            ⚠️ **券商端的查詢方法與欄位尚未以模擬環境核對**（規劃要求實作前核對）。
            取不到時只記 warning 並略過——盤後少一次回填不影響部位，
            而在這裡拋例外會讓報表也產不出來。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                成功回填的筆數
        """

        provider: Optional[Callable[[datetime.date], List[Any]]] = getattr(
            self.broker, "get_profit_loss_details", None
        )
        if provider is None:
            logger.warning(
                "券商閘道尚未提供損益明細查詢，本次不回填實際費用；"
                "成本統計會停留在估算值"
            )
            return 0

        try:
            details: List[Any] = provider(run_date)
        except Exception as exc:
            logger.opt(exception=True).warning(f"回填實際費用失敗（略過）：{exc}")
            return 0

        filled: int = 0
        for detail in details:
            seqno: str = str(getattr(detail, "seqno", "") or "")
            trade_id: str = str(getattr(detail, "trade_id", "") or "")
            if not seqno or not trade_id:
                continue
            self.dao.backfill_fill_costs(
                seqno,
                trade_id,
                float(getattr(detail, "fee", 0.0) or 0.0),
                float(getattr(detail, "tax", 0.0) or 0.0),
            )
            filled += 1

        logger.info(f"回填實際費用 {filled} 筆")
        return filled

    def handle_unfilled_remainders(self, run_date: datetime.date) -> int:
        """
        - Description:
            未成交殘量的處理政策

            **開倉與出場的處理完全不同**：
            - **開倉未成交一律放棄**，不追價。追價等於在偏離訊號價的位置建倉，
              而回測沒有這個行為。
            - **平倉與停損未成交必須補**：那是預期外的隔夜部位，風險遠大於
              開倉沒成交。寫一筆 `PENDING` 待辦，由次日開盤段第一件事執行。

            **待辦要有狀態才冪等**：只記「明天要補」而沒有完成標記的話，
            次日開盤段重跑或崩潰重啟會重複送補平單——而重複的補平單不是多買一點，
            是直接把部位做反。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                新增的待辦筆數
        """

        created: int = 0
        for order_row in self.dao.get_orders_by_date(run_date):
            remainder: int = int(order_row.get("volume") or 0) - int(
                order_row.get("filled_volume") or 0
            )
            if remainder <= 0:
                continue

            if not self._is_exit_row(order_row):
                logger.info(
                    f"開倉單 {order_row['client_order_id']} 殘量 {remainder} 放棄，"
                    "不追價（追價等於在偏離訊號價的位置建倉）"
                )
                continue

            self._record_pending_cover(order_row, remainder, run_date)
            created += 1

        return created

    def _record_pending_cover(
        self, order_row: Dict[str, Any], remainder: int, run_date: datetime.date
    ) -> None:
        """寫一筆待辦與一則 CRITICAL 事件，並推播"""

        client_order_id: str = str(order_row["client_order_id"])
        message: str = (
            f"平倉／停損單 {client_order_id}（{order_row['symbol']}）殘量 {remainder} "
            "未成交，已成為預期外的隔夜部位；次日開盤段第一件事補平"
        )
        logger.error(message)

        self.dao.insert_pending_action(
            {
                "action_id": f"{run_date.isoformat()}-{client_order_id}",
                "strategy_name": order_row["strategy_name"],
                "symbol": order_row["symbol"],
                "action": order_row["action"],
                "position_type": order_row["position_type"],
                "volume": remainder,
                "due_date": run_date + datetime.timedelta(days=1),
                "status": self.dao.ACTION_PENDING,
                "reason": "平倉單未成交",
                "source_client_order_id": client_order_id,
                "created_at": self._now(),
            }
        )
        self.dao.insert_risk_event(
            {
                "run_id": self.run_id,
                "strategy_name": order_row["strategy_name"],
                "severity": "CRITICAL",
                "category": "UNFILLED_EXIT",
                "symbol": order_row["symbol"],
                "client_order_id": client_order_id,
                "message": message,
                "occurred_at": self._now(),
            }
        )
        self._notify("CRITICAL", "平倉單未成交", message)

    @staticmethod
    def _is_exit_row(order_row: Dict[str, Any]) -> bool:
        """
        這張委託是不是出場單

        以持倉方向與買賣別推導，與 `order_preprocess.resolve_close_action()` 同一套
        規則——散在多處會漂移，而漂移的後果是開倉單被當成平倉單去補，
        那會憑空建出一個新部位。
        """

        position_type: PositionType = PositionType(str(order_row["position_type"]))
        return str(order_row["action"]) == (
            order_preprocess.resolve_close_action(position_type).value
        )

    def _notify(self, level: str, title: str, body: str) -> None:
        """推播；失敗一律吞掉，監控不可拖垮被監控的東西"""

        notify_safely(self.notifier, level, title, body)
