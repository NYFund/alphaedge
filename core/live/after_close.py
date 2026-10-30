import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd
from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.account_sync import AccountSynchronizer
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.fill_backfill import FillBackfiller
from core.live.notify.base import BaseNotifier, NotifyLevel, notify_safely
from core.live.oms.order_manager import OrderManager
from core.live.reconciler import Reconciler
from core.live.report.live_reporter import LiveReporter
from core.live.report.parity_checker import ParityChecker, ParityDiff
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.trading_mode import TradingModeState
from core.models import RealizedTradeSnapshot
from core.portfolio import order_rules
from core.utils import Action, LiveOrderStatus, Units

"""
盤後作業：把當天發生的事收攏成可稽核的結果

**與送單段落沒有共用任何狀態**，所以獨立成一個類別而不是留在 `LiveTrader` 裡：
它自己 `connect()` / `finally: close()`，不碰資金保留、段落時窗與跨策略守門，
實盤入口（`apps/live.py`）也是以 `--phase after_close` 走完全獨立的分支。

**只負責「寫下明天要補的事」，不負責執行它**：`apply_pending_actions()` 留在
`LiveTrader`，因為待辦是在**開盤段**被補平的。寫入端與執行端本來就不對稱，
硬抽成一個服務會讓兩邊都得繞一層。
"""


# 依成本模型重算一筆已平倉交易的費用與稅：`(交易, 開倉成交, 交易日) → 金額`。
# **市場特性**（股票證交稅只課賣出、期貨兩邊都課），由組裝層依市場注入；
# 算不出來（缺開倉成交、乘數未知）時回 None
TradeCostEstimator = Callable[
    [RealizedTradeSnapshot, List[Dict[str, Any]], datetime.date], Optional[float]
]

# 券商實際成本與估算值的差距超過這個比例就寫事件
COST_DRIFT_RATIO: float = 0.2


def pending_cover_action_id(run_date: datetime.date, client_order_id: str) -> str:
    """
    一張出場單的次日補平待辦編號

    **段落內與盤後兩條路要算出同一個編號**：送單途中拋例外的出場單，段落內
    （`ExecutionEngine._record_unsent_exit()`）與盤後掃殘量（`handle_unfilled_remainders()`）
    都會替它排補平，編號不同就是兩筆待辦、次日兩張補平單。
    """

    return f"{run_date.isoformat()}-{client_order_id}"


def weighted_fill_price(fills: List[Dict[str, Any]]) -> float:
    """一張委託各筆成交的量加權均價"""

    volume: int = sum(int(fill["volume"]) for fill in fills)
    if volume <= 0:
        return 0.0
    return sum(float(fill["price"]) * int(fill["volume"]) for fill in fills) / volume


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
        cost_estimator: Optional[TradeCostEstimator] = None,
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
            - cost_estimator: Optional[TradeCostEstimator]
                依成本模型重算費用與稅的估算器；None 時不做成本校正
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
        self.cost_estimator: Optional[TradeCostEstimator] = cost_estimator
        self._now: Callable[[], datetime.datetime] = now_provider
        # 風控事件的唯一寫入口。**自建而不是注入**：本類已經持有
        # `(dao, run_id, now_provider)` 三件組，注入只是把同一組東西再傳一次，
        # 卻要改動建構子簽名與每一個建這個類別的地方
        self.events: RiskEventLogger = RiskEventLogger(
            self.dao, self.run_id, now_provider=self._now
        )
        self.backfiller: FillBackfiller = FillBackfiller(
            order_manager, account_sync, dao, run_id, now_provider, notifier
        )
        # 券商刷新回來的累計成交量（client_order_id → 量），殘量處理用；見 `expire_open_orders()`
        self.broker_filled: Dict[str, int] = {}

        # 本次對帳結果；實盤入口（`apps/live.py`）由它決定退出碼，故盤後跑完要回填給 `LiveTrader`
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

            # 0. 接管當日委託：盤後是獨立行程，委託管理器手上沒有任何 ticket，
            #    不先由紀錄庫重建並向券商對一次，後面的刷新沒有對象、標撤會把券商端
            #    已成交的出場單標成未成交，殘量處理再對已經平掉的部位排次日補平
            self.recover_orders(today)

            # 1. 成交事實先於狀態：另一個行程先收到的成交回報已在 `live_fill`，
            #    本行程的委託卻可能還停在送出狀態。不先回填的話，下一步會把已成交的單
            #    標成「日終未成交」，殘量處理再對已經平掉的部位排補平
            self.backfill_fills(today)

            # 2. 刷新委託狀態（券商成交多於本地時寫事件），再把仍未終結的標成已撤：
            #    ROD 未成交單在券商端日終自動失效
            self.expire_open_orders()

            # 3. 對帳與快照
            positions: List[Any] = self.broker.get_positions()
            self.account_sync.rebuild_from_broker(positions)
            self.last_reconcile = self.reconciler.check(positions)

            # 4. 以券商的已實現損益校正成本估算（差額是校正成本設定的依據）
            self.calibrate_costs(today)

            # 5. 未成交殘量依政策處理
            remainders: int = self.handle_unfilled_remainders(today)

            # 6. 報表與滑價
            reports: Dict[str, Path] = self.reporter.write_daily_reports(today)
            slippage: Dict[str, float] = self.reporter.summarize_slippage(today)

            # 訊號 parity **不在這裡比**：當日回測要用當天的日 K，而它要到次日早上
            # 資料更新後才入庫。在這裡比，回測一張委託都產生不出來，實盤的開倉單
            # 全被歸成快照口徑差異，parity 每天都「通過」。改由次日資料更新後
            # 以 `--phase parity` 補比（見 `LiveTrader.run_parity()`）
            logger.info("訊號 parity 於次日資料更新後補比，盤後不比對")

            return {
                "reports": reports,
                "pending_actions": remainders,
                "slippage": slippage,
            }
        finally:
            self.broker.close()
            for feed in self.data_feeds:
                feed.close()
            logger.info("=== 盤後作業結束 ===")

    def check_signal_parity(self, run_date: datetime.date) -> int:
        """
        - Description:
            比對某一交易日的實盤委託與同一天回測會送出的委託

            **只在該日的日 K 已入庫後呼叫**（次日資料更新後的補比），盤後當下不呼叫——
            理由見 `run()`。

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
            self._notify(NotifyLevel.CRITICAL, "訊號 parity 比對失敗", str(exc))
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
            NotifyLevel.CRITICAL,
            "訊號 parity 有未解釋的差異",
            f"{run_date} 共 {total} 筆（{detail}），列為隔日第一優先",
        )
        return total

    def recover_orders(self, run_date: datetime.date) -> List[Any]:
        """
        - Description:
            由紀錄庫重建本行程策略的當日委託，並向券商對一次狀態與序號

            與段落啟動時的接管是同一支 `OrderManager.recover()`：送單逾時被標失敗、
            券商其實受理的單（2026-10-20 的換月開倉腿）在這裡對回序號，接著的成交回填
            才找得到它的成交。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Any]
                接管後的委託
        """

        return self.order_manager.recover(run_date)

    def expire_open_orders(self) -> List[Any]:
        """
        - Description:
            向券商刷新一次委託，再把當日仍未終結的委託標成已撤

            ROD 單在券商端日終自動失效，**本地要跟著標**：不標的話，
            明天的恢復流程會把它們當成「還在場上」去接管，然後撤一張不存在的單。

            刷新回來的券商累計成交量留著給殘量處理：券商成交多於本地時，差額不是
            「未成交」而是「本地沒收到回報」（已寫 `FILL_REPORT_MISSING`），
            照本地數字排補平會對已經平掉的部位再平一次。
        - Return:
            - List[Any]
                被標記的委託
        """

        broker_tickets: List[Any] = self.order_manager.refresh_from_broker()
        by_seqno: Dict[str, int] = {
            ticket.broker_seqno: int(ticket.filled_volume or 0)
            for ticket in broker_tickets
            if ticket.broker_seqno
        }
        self.broker_filled = {
            client_order_id: by_seqno[ticket.broker_seqno]
            for client_order_id, ticket in self.order_manager.tickets.items()
            if ticket.broker_seqno and ticket.broker_seqno in by_seqno
        }
        return self.order_manager.expire_unfinished(self._now().date())

    def backfill_fills(self, run_date: datetime.date) -> int:
        """
        - Description:
            以 `live_fill` 的成交事實回填本行程策略的委託與歸屬帳（`FillBackfiller`）

            **一定排在刷新與殘量處理之前**：順序反了，已成交的單會被標成日終未成交，
            再對已經平掉的部位排補平——2026-10-13 的實況。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                被回填的委託數
        """

        return self.backfiller.backfill(run_date)

    def calibrate_costs(self, run_date: datetime.date) -> List[Dict[str, Any]]:
        """
        - Description:
            以券商的已實現損益，逐筆比對實際費用與成本模型的估算，寫成報表

            **以「一筆已平倉交易」為單位，不回填逐筆成交**：券商不提供逐筆費用
            （2026-09-22 模擬環境實測：股票只給淨損益，期貨有費用與稅但沒有委託序號）。
            - 期貨：實際成本 ＝ 券商的 `fee + tax`。
            - 股票：實際成本 ＝ 以本地開倉成交價算的毛損益 − 券商淨損益；
              開倉價以券商給的開倉委託序號回查 `live_fill`。
            估算值由注入的估算器依同一筆交易的開平倉價重算。任一邊算不出來時
            該列照樣寫出並註明原因，不略過——略過的話報表看起來全都對得上。
            差距超過 `COST_DRIFT_RATIO` 時寫 `COST_MODEL_DRIFT` 事件。

            **本身失敗只記 warning 不往外拋**：校正是事後分析，拋出去會讓報表也產不出來。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - List[Dict[str, Any]]
                比對結果（每筆已平倉交易一列）
        """

        provider: Optional[Callable[[datetime.date], List[Any]]] = getattr(
            self.broker, "get_realized_trades", None
        )
        if provider is None or self.cost_estimator is None:
            logger.info("券商閘道或成本估算器未提供，本次不校正成本")
            return []

        try:
            trades: List[RealizedTradeSnapshot] = list(provider(run_date))
        except Exception as exc:
            logger.opt(exception=True).warning(f"查詢已實現損益失敗（略過校正）：{exc}")
            return []

        rows: List[Dict[str, Any]] = [
            self._calibrate_trade(trade, run_date) for trade in trades
        ]
        if rows:
            self._write_calibration(rows, run_date)
        logger.info(f"成本校正 {len(rows)} 筆")
        return rows

    def _calibrate_trade(
        self, trade: RealizedTradeSnapshot, run_date: datetime.date
    ) -> Dict[str, Any]:
        """比對一筆已平倉交易；超過門檻時寫事件"""

        # 券商只給開倉委託序號、不給開倉日；序號每天從頭配發，取平倉日以前最近一次的那天
        opening: List[Dict[str, Any]] = (
            self.dao.get_latest_fills_by_seqno(trade.open_seqno, run_date)
            if trade.open_seqno
            else []
        )
        actual: Optional[float] = self._actual_cost(trade, opening)
        estimated: Optional[float] = self.cost_estimator(trade, opening, run_date)

        note: str = ""
        if actual is None:
            note = "開倉成交不在本地紀錄，算不出實際成本"
        elif estimated is None:
            note = "成本模型算不出估算值"

        diff: Optional[float] = (
            actual - estimated if actual is not None and estimated is not None else None
        )
        ratio: Optional[float] = (
            diff / estimated if diff is not None and estimated else None
        )

        if ratio is not None and abs(ratio) > COST_DRIFT_RATIO:
            message: str = (
                f"{trade.symbol} 實際成本 {actual:,.0f}、估算 {estimated:,.0f}，"
                f"差 {ratio:+.0%}：成本設定可能需要校正"
            )
            logger.warning(message)
            self.events.write(
                category="COST_MODEL_DRIFT",
                severity=NotifyLevel.WARN,
                message=message,
                symbol=trade.symbol,
            )

        return {
            "date": run_date.isoformat(),
            "symbol": trade.symbol,
            "quantity": trade.quantity,
            "broker_pnl": trade.pnl,
            "actual_cost": actual,
            "estimated_cost": estimated,
            "diff": diff,
            "diff_ratio": ratio,
            "note": note,
        }

    @staticmethod
    def _actual_cost(
        trade: RealizedTradeSnapshot, opening: List[Dict[str, Any]]
    ) -> Optional[float]:
        """券商實際扣的費用與稅；算不出來時為 None"""

        if trade.fee is not None and trade.tax is not None:
            return float(trade.fee + trade.tax)
        if not opening:
            return None

        entry: float = weighted_fill_price(opening)
        opened_long: bool = str(opening[0]["action"]) == Action.BUY.value
        per_unit: float = (
            trade.cover_price - entry if opened_long else entry - trade.cover_price
        )
        # 股票已實現交易的數量以張計、價格以股計
        gross: float = per_unit * trade.quantity * Units.LOT
        return gross - trade.pnl

    def _write_calibration(
        self, rows: List[Dict[str, Any]], run_date: datetime.date
    ) -> Path:
        """寫到盤後報表目錄的 `account/`（帳戶層，不屬於任何一支策略）"""

        directory: Path = self.reporter.output_root / "account"
        directory.mkdir(parents=True, exist_ok=True)
        path: Path = directory / f"{run_date.isoformat()}_cost_calibration.csv"
        pd.DataFrame(rows).to_csv(path, index=False)
        return path

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

            **同一張單只能有一筆待辦**：送單途中拋例外的出場單已經在段落內以
            `{日期}-{client_order_id}` 排過補平（`ExecutionEngine._record_unsent_exit()`），
            這裡掃到它的殘量時用同一個 `action_id`，紀錄庫會擋下第二筆；
            兩筆待辦在次日開盤段會各送一張，把部位做反。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - int
                新增的待辦筆數
        """

        created: int = 0
        # 只掃本行程策略的委託：股票線與期貨線共用紀錄庫，掃全部會拿另一條線的委託
        # 替它排待辦、重複推播，而那條線自己的盤後也會再排一次
        for order_row in self.order_manager.orders_of_day(run_date):
            client_order_id: str = str(order_row["client_order_id"])
            # **已是 FILLED 的沒有殘量**：FILLED 只會由券商的事實推到（成交回報、接管、刷新），
            # 本地成交量仍不足代表回報漏接（刷新已寫 `FILL_REPORT_MISSING`），
            # 不是沒成交。券商刷新回來的累計量同理優先於本地
            if str(order_row.get("status")) == LiveOrderStatus.FILLED.value:
                continue
            filled: int = max(
                int(order_row.get("filled_volume") or 0),
                self.broker_filled.get(client_order_id, 0),
            )
            remainder: int = int(order_row.get("volume") or 0) - filled
            if remainder <= 0:
                continue

            if not order_rules.is_closing_record(
                str(order_row["position_type"]), str(order_row["action"])
            ):
                logger.info(
                    f"開倉單 {order_row['client_order_id']} 殘量 {remainder} 放棄，"
                    "不追價（追價等於在偏離訊號價的位置建倉）"
                )
                continue

            if self._record_pending_cover(order_row, remainder, run_date):
                created += 1

        return created

    def _record_pending_cover(
        self, order_row: Dict[str, Any], remainder: int, run_date: datetime.date
    ) -> bool:
        """寫一筆待辦與一則 CRITICAL 事件，並推播；這張單已排過待辦時回 False、什麼都不寫"""

        client_order_id: str = str(order_row["client_order_id"])
        message: str = (
            f"平倉／停損單 {client_order_id}（{order_row['symbol']}）殘量 {remainder} "
            "未成交，已成為預期外的隔夜部位；次日開盤段第一件事補平"
        )

        is_new: bool = self.dao.insert_pending_action(
            {
                "action_id": pending_cover_action_id(run_date, client_order_id),
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
        if not is_new:
            logger.info(
                f"平倉／停損單 {client_order_id} 的補平待辦已在段落內排過，不重複寫入"
            )
            return False

        logger.error(message)
        self.events.write(
            category="UNFILLED_EXIT",
            severity=NotifyLevel.CRITICAL,
            message=message,
            strategy_name=order_row["strategy_name"],
            symbol=order_row["symbol"],
            client_order_id=client_order_id,
        )
        self._notify(NotifyLevel.CRITICAL, "平倉單未成交", message)
        return True

    def _notify(self, level: NotifyLevel, title: str, body: str) -> None:
        """推播；失敗一律吞掉，監控不可拖垮被監控的東西"""

        notify_safely(self.notifier, level, title, body)
