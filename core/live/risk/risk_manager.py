import datetime
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.config.paths import LIVE_KILL_SWITCH_PATH
from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.execution.order_preprocess import exceeds_symbol_exposure
from core.live.notify.base import NotifyLevel
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.risk_config import RiskConfig
from core.live.risk.trading_mode import TradingMode, TradingModeState
from core.models import BaseOrder
from core.utils import Action, PositionType

"""
事前風控：策略或程式出錯時，損失要有硬上限

本檔的規則分成兩種性質，而且**刻意用不同的形狀寫**：

| 性質 | 規則 | 寫成 |
|------|------|------|
| 純判定 | 單筆金額、單日累計金額、張數口數、委託價偏離、批次曝險、單一標的占比 | **模組層級純函式**（訂單 ＋ 帳戶快照 ＋ 設定 → 過或不過） |
| 依賴行程狀態 | 下單頻率、kill switch、`TradingMode`、當日損益 | `PreTradeRiskManager` 的方法 |

**為什麼現在就分**：回測端目前沒有、也不需要事前風控。但日後若要讓回測**事前**
評估「這支策略在實盤風控下會被擋掉多少」（現在只能靠盤後 parity 事後量），
純判定那幾條可以直接重用；綁死在長駐狀態上就得再搬一次——`core/portfolio/sizing.py`
當初從 `core/backtest/models/` 搬過來，就是同一個錯誤的代價。

**kill switch 的檢查時點寫死在送單路徑上**：每張單送出前檢查一次。
只在啟動時檢查等於沒有 kill switch——真正需要它的時候，程式早就已經在跑了。
"""


@dataclass(frozen=True)
class RiskDecision:
    """單筆風控判定結果"""

    passed: bool
    category: str = ""
    reason: str = ""

    @staticmethod
    def ok() -> "RiskDecision":
        """通過"""

        return RiskDecision(passed=True)

    @staticmethod
    def reject(category: str, reason: str) -> "RiskDecision":
        """拒絕；`category` 會寫進 `live_risk_event`，供日後統計拒單原因分布"""

        return RiskDecision(passed=False, category=category, reason=reason)


@dataclass(frozen=True)
class ExposureItem:
    """批次曝險試算的單筆輸入：一張委託對應的金額"""

    order: BaseOrder
    amount: float


# === 純判定：只要「訂單 ＋ 帳戶快照 ＋ 設定」就算得出過或不過 ===
def check_single_order_amount(
    amount: float, init_capital: float, config: RiskConfig
) -> RiskDecision:
    """
    - Description:
        單筆委託金額上限
    - Parameters:
        - amount: float
            本筆委託金額（股票：價 × 股數；期貨：保證金 × 口數）
        - init_capital: float
            該策略的資金額度上限
        - config: RiskConfig
            風控設定
    - Return:
        - RiskDecision
            判定結果
    """

    cap: float = init_capital * config.single_order_amount_ratio
    if amount > cap:
        return RiskDecision.reject(
            "SINGLE_ORDER_AMOUNT",
            f"單筆委託金額 {amount:,.0f} 超過上限 {cap:,.0f}"
            f"（額度 {init_capital:,.0f} 的 {config.single_order_amount_ratio:.0%}）",
        )
    return RiskDecision.ok()


def check_daily_amount(
    used_amount: float, amount: float, init_capital: float, config: RiskConfig
) -> RiskDecision:
    """
    - Description:
        單日累計委託金額上限

        這是**流量**指標，防的是迴圈 bug 或重啟造成的反覆送單。
        它與批次曝險（存量）管的是不同的東西，只留其中一條都有破口：
        一天之內反覆買進賣出同一檔，曝險始終很低，累計金額卻早就爆了。
    - Parameters:
        - used_amount: float
            今日已送出的累計委託金額
        - amount: float
            本筆委託金額
        - init_capital: float
            該策略的資金額度上限
        - config: RiskConfig
            風控設定
    - Return:
        - RiskDecision
            判定結果
    """

    cap: float = init_capital * config.daily_amount_ratio
    if used_amount + amount > cap:
        return RiskDecision.reject(
            "DAILY_AMOUNT",
            f"單日累計委託金額 {used_amount + amount:,.0f} 超過上限 {cap:,.0f}",
        )
    return RiskDecision.ok()


def check_volume_cap(volume: int, is_futures: bool, config: RiskConfig) -> RiskDecision:
    """
    - Description:
        單筆張數／口數上限
    - Parameters:
        - volume: int
            委託數量
        - is_futures: bool
            是否為期貨（口數上限與股票張數不同）
        - config: RiskConfig
            風控設定
    - Return:
        - RiskDecision
            判定結果
    """

    cap: int = config.max_futures_contracts if is_futures else config.max_stock_lots
    unit: str = "口" if is_futures else "張"
    if volume > cap:
        return RiskDecision.reject(
            "VOLUME_CAP", f"單筆數量 {volume} {unit}超過上限 {cap} {unit}"
        )
    return RiskDecision.ok()


def check_price_deviation(
    price: Optional[float],
    reference_price: float,
    config: RiskConfig,
    limit_up: Optional[float] = None,
    limit_down: Optional[float] = None,
) -> RiskDecision:
    """
    - Description:
        委託價偏離檢查

        **市價單沒有價格可檢查**（`price` 為 None 或 0）：改以送單前的基準價
        做事前檢查，成交後再由盤後流程以實際成交價回頭比對。
        故這裡對市價單一律放行，並由呼叫端負責事後比對——**在這裡擋不住的東西，
        不要假裝擋得住**。
    - Parameters:
        - price: Optional[float]
            委託價；市價單為 None 或 0
        - reference_price: float
            基準價（盤前用參考價、盤中用最新成交價）
        - config: RiskConfig
            風控設定
        - limit_up: Optional[float]
            漲停價（交易所公告值）
        - limit_down: Optional[float]
            跌停價
    - Return:
        - RiskDecision
            判定結果
    """

    if not price:
        return RiskDecision.ok()

    if limit_up and price > limit_up:
        return RiskDecision.reject(
            "PRICE_LIMIT", f"委託價 {price} 高於漲停價 {limit_up}"
        )
    if limit_down and price < limit_down:
        return RiskDecision.reject(
            "PRICE_LIMIT", f"委託價 {price} 低於跌停價 {limit_down}"
        )

    if reference_price <= 0:
        return RiskDecision.ok()

    deviation: float = abs(price - reference_price) / reference_price
    if deviation > config.price_deviation_ratio:
        return RiskDecision.reject(
            "PRICE_DEVIATION",
            f"委託價 {price} 偏離基準價 {reference_price} 達 {deviation:.1%}，"
            f"超過上限 {config.price_deviation_ratio:.1%}",
        )
    return RiskDecision.ok()


def truncate_batch_by_exposure(
    items: Sequence[ExposureItem],
    existing_exposure: float,
    existing_symbol_exposure: Dict[str, float],
    init_capital: float,
    config: RiskConfig,
) -> Tuple[List[ExposureItem], List[Tuple[ExposureItem, str]]]:
    """
    - Description:
        批次曝險試算：逐單都過、加總超額，是日頻一次送多張單時的典型破口

        **超額時截斷而非整批拒絕**：整批拒會讓平倉單也被擋掉，那比超額更危險。
        **平倉單一律放行且不計入曝險**——它讓曝險變小，擋它沒有道理。

        輸入順序即優先順序（呼叫端已依 `sort_orders()` 排好），被截斷的是後面的單。
    - Parameters:
        - items: Sequence[ExposureItem]
            本批委託與各自的金額
        - existing_exposure: float
            送出前的既有曝險（持倉市值 ＋ 在途委託）
        - existing_symbol_exposure: Dict[str, float]
            各標的的既有曝險
        - init_capital: float
            該策略的資金額度上限
        - config: RiskConfig
            風控設定
    - Return:
        - Tuple[List[ExposureItem], List[Tuple[ExposureItem, str]]]
            （放行清單, [(被截斷的單, 原因)]）
    """

    total_cap: float = init_capital * config.total_exposure_ratio
    # 單一標的上限的公式與回測共用；差異（預設值、適用方向、超限行為）
    # 寫在 `order_preprocess.exceeds_symbol_exposure()` 的 docstring
    symbol_ratio: float = config.single_symbol_exposure_ratio
    symbol_cap: float = init_capital * symbol_ratio

    allowed: List[ExposureItem] = []
    truncated: List[Tuple[ExposureItem, str]] = []
    running: float = existing_exposure
    per_symbol: Dict[str, float] = dict(existing_symbol_exposure)

    for item in items:
        if is_closing_order(item.order):
            allowed.append(item)
            continue

        symbol: str = item.order.symbol
        if running + item.amount > total_cap:
            truncated.append(
                (
                    item,
                    f"送出後總曝險 {running + item.amount:,.0f} 超過上限 {total_cap:,.0f}",
                )
            )
            continue
        if exceeds_symbol_exposure(
            per_symbol.get(symbol, 0.0) + item.amount, init_capital, symbol_ratio
        ):
            truncated.append(
                (
                    item,
                    f"{symbol} 曝險 {per_symbol.get(symbol, 0.0) + item.amount:,.0f} "
                    f"超過單一標的上限 {symbol_cap:,.0f}",
                )
            )
            continue

        allowed.append(item)
        running += item.amount
        per_symbol[symbol] = per_symbol.get(symbol, 0.0) + item.amount

    return (allowed, truncated)


def is_closing_order(order: BaseOrder) -> bool:
    """
    - Description:
        判斷是否為平倉單

        LONG 的賣出、SHORT 的買進都是平倉。**這條判定散在多處會漂移**，
        而漂移的後果是平倉單被當成開倉擋掉——部位因此失去出場能力。
    - Parameters:
        - order: BaseOrder
            訂單
    - Return:
        - bool
            是否為平倉
    """

    if order.position_type is PositionType.LONG:
        return order.action is Action.SELL
    return order.action is Action.BUY


# === 依賴行程狀態的規則 ===
class PreTradeRiskManager:
    """
    - Description:
        事前風控：每張委託送出前的最後一道關卡

        **`TradingMode` 由它單一持有**。對帳器、盤中事件迴圈、券商 session
        都只送降級事件過來，不自己改模式——散在各元件各自切換的話，
        沒有任何一處知道「現在到底能不能送單」。
    """

    def __init__(
        self,
        mode_state: TradingModeState,
        config: Optional[RiskConfig] = None,
        dao: Optional[LiveTradeDAO] = None,
        run_id: str = "",
        kill_switch_path: Path = LIVE_KILL_SWITCH_PATH,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立風控
        - Parameters:
            - mode_state: TradingModeState
                交易模式狀態機（風控是它的唯一持有者）
            - config: Optional[RiskConfig]
                風控設定；None 時用預設值
            - dao: Optional[LiveTradeDAO]
                紀錄庫；拒單與降級事件寫進 `live_risk_event`
            - run_id: str
                本次啟動的識別碼
            - kill_switch_path: Path
                kill switch 檔案路徑；**檔案存在即停止送單**
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.mode_state: TradingModeState = mode_state
        self.config: RiskConfig = config if config is not None else RiskConfig()
        self.dao: Optional[LiveTradeDAO] = dao
        self.run_id: str = run_id
        self.kill_switch_path: Path = kill_switch_path
        self._now: Callable[[], datetime.datetime] = now_provider
        # 風控事件的唯一寫入口。**自建而不是注入**：本類已經持有
        # `(dao, run_id, now_provider)` 三件組，注入只是把同一組東西再傳一次，
        # 卻要改動建構子簽名與每一個建這個類別的地方
        self.events: RiskEventLogger = RiskEventLogger(
            self.dao, self.run_id, now_provider=self._now
        )

        # 逐策略的當日累計委託金額（流量）與送單時刻（頻率）
        self.daily_amount: Dict[str, float] = {}
        self._recent_orders: Dict[str, Deque[datetime.datetime]] = {}

    # === 主檢查 ===
    def check(
        self,
        order: BaseOrder,
        strategy_name: str,
        init_capital: float,
        amount: float,
        reference_price: float = 0.0,
        limit_up: Optional[float] = None,
        limit_down: Optional[float] = None,
        is_futures: bool = False,
    ) -> RiskDecision:
        """
        - Description:
            逐單檢查；任何一條不過就拒單並寫 `live_risk_event`

            檢查順序是「先看能不能送、再看該不該送」：kill switch 與交易模式
            在最前面，因為它們一旦成立，後面算得再準也沒有意義。
        - Parameters:
            - order: BaseOrder
                待送出的訂單
            - strategy_name: str
                歸屬策略
            - init_capital: float
                該策略的資金額度上限
            - amount: float
                本筆委託金額
            - reference_price: float
                基準價
            - limit_up: Optional[float]
                漲停價
            - limit_down: Optional[float]
                跌停價
            - is_futures: bool
                是否為期貨
        - Return:
            - RiskDecision
                判定結果
        """

        decision: RiskDecision = self._check_gates(order, strategy_name)
        if decision.passed:
            decision = self._check_limits(
                order,
                strategy_name,
                init_capital,
                amount,
                reference_price,
                limit_up,
                limit_down,
                is_futures,
            )

        if not decision.passed:
            self.record_rejection(order, strategy_name, decision)
            return decision

        self._record_submission(strategy_name, amount)
        return decision

    def _check_gates(self, order: BaseOrder, strategy_name: str) -> RiskDecision:
        """kill switch 與交易模式；兩者一旦成立，後面算得再準也沒有意義"""

        if self.is_kill_switch_on():
            self.mode_state.degrade(
                TradingMode.HALTED, f"kill switch 檔案存在：{self.kill_switch_path}"
            )
            return RiskDecision.reject("KILL_SWITCH", "kill switch 生效，拒絕所有委託")

        closing: bool = is_closing_order(order)
        if closing and not self.mode_state.allows_close(strategy_name):
            return RiskDecision.reject(
                "MODE_HALTED",
                f"交易模式為 {self.mode_state.effective_mode(strategy_name).value}，連平倉都不送",
            )
        if not closing and not self.mode_state.allows_open(strategy_name):
            return RiskDecision.reject(
                "MODE_REDUCE_ONLY",
                f"交易模式為 {self.mode_state.effective_mode(strategy_name).value}，只允許平倉",
            )
        return RiskDecision.ok()

    def _check_limits(
        self,
        order: BaseOrder,
        strategy_name: str,
        init_capital: float,
        amount: float,
        reference_price: float,
        limit_up: Optional[float],
        limit_down: Optional[float],
        is_futures: bool,
    ) -> RiskDecision:
        """金額、數量、價格與頻率"""

        for decision in (
            check_single_order_amount(amount, init_capital, self.config),
            check_daily_amount(
                self.daily_amount.get(strategy_name, 0.0),
                amount,
                init_capital,
                self.config,
            ),
            check_volume_cap(order.volume, is_futures, self.config),
            check_price_deviation(
                getattr(order, "price", None),
                reference_price,
                self.config,
                limit_up,
                limit_down,
            ),
            self._check_order_rate(strategy_name),
        ):
            if not decision.passed:
                return decision
        return RiskDecision.ok()

    def _check_order_rate(self, strategy_name: str) -> RiskDecision:
        """
        下單頻率

        **超過就降級到 `REDUCE_ONLY`，不只是拒單**：每分鐘送出幾十張單的策略
        通常是迴圈 bug，而 bug 不會因為被拒一次就停下來。
        """

        window: Deque[datetime.datetime] = self._recent_orders.setdefault(
            strategy_name, deque()
        )
        now: datetime.datetime = self._now()
        cutoff: datetime.datetime = now - datetime.timedelta(minutes=1)
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) >= self.config.max_orders_per_minute:
            reason: str = (
                f"{strategy_name} 一分鐘內送出 {len(window)} 張委託，"
                f"超過上限 {self.config.max_orders_per_minute}"
            )
            self.mode_state.degrade(
                TradingMode.REDUCE_ONLY, reason, strategy_name=strategy_name
            )
            return RiskDecision.reject("ORDER_RATE", reason)
        return RiskDecision.ok()

    def _record_submission(self, strategy_name: str, amount: float) -> None:
        """通過後才計入流量與頻率：被拒的單沒有送出去，不該佔額度"""

        self.daily_amount[strategy_name] = (
            self.daily_amount.get(strategy_name, 0.0) + amount
        )
        self._recent_orders.setdefault(strategy_name, deque()).append(self._now())

    # === 批次 ===
    def check_batch(
        self,
        items: Sequence[ExposureItem],
        strategy_name: str,
        init_capital: float,
        existing_exposure: float = 0.0,
        existing_symbol_exposure: Optional[Dict[str, float]] = None,
    ) -> List[ExposureItem]:
        """
        - Description:
            批次曝險檢查；超額的**截斷**而非整批拒絕

            整批拒會讓平倉單也被擋掉，那比超額更危險。
        - Parameters:
            - items: Sequence[ExposureItem]
                本批委託（順序即優先順序）
            - strategy_name: str
                歸屬策略
            - init_capital: float
                該策略的資金額度上限
            - existing_exposure: float
                送出前的既有曝險
            - existing_symbol_exposure: Optional[Dict[str, float]]
                各標的的既有曝險
        - Return:
            - List[ExposureItem]
                放行的委託
        """

        allowed, truncated = truncate_batch_by_exposure(
            items,
            existing_exposure,
            existing_symbol_exposure or {},
            init_capital,
            self.config,
        )
        for item, reason in truncated:
            self.record_rejection(
                item.order,
                strategy_name,
                RiskDecision.reject("BATCH_EXPOSURE", reason),
            )
        return allowed

    # === 損益與降級 ===
    def check_daily_loss(
        self, strategy_name: str, loss: float, init_capital: float
    ) -> bool:
        """
        - Description:
            單日已實現＋未實現虧損；超過就把**該策略**降到 `REDUCE_ONLY`

            **日頻模式下這條的粒度只有段落級**：一天只在開盤段與尾盤段各算一次，
            盤中跌破門檻不會即時觸發。那是日頻策略本來就有的性質（回測也是 bar 級停損），
            但不要誤以為有盤中保護。
        - Parameters:
            - strategy_name: str
                策略名
            - loss: float
                虧損金額（正數表示虧損）
            - init_capital: float
                該策略的資金額度上限
        - Return:
            - bool
                是否觸發降級
        """

        cap: float = init_capital * self.config.daily_loss_ratio
        if loss <= cap:
            return False

        reason: str = f"{strategy_name} 當日虧損 {loss:,.0f} 超過上限 {cap:,.0f}"
        self.mode_state.degrade(
            TradingMode.REDUCE_ONLY, reason, strategy_name=strategy_name
        )
        self._write_event("DAILY_LOSS", NotifyLevel.CRITICAL, reason, strategy_name)
        return True

    def check_account_daily_loss(self, loss: float, total_equity: float) -> bool:
        """
        - Description:
            帳戶當日虧損；超過就把**全體**策略降到 `REDUCE_ONLY`
        - Parameters:
            - loss: float
                帳戶虧損金額（正數表示虧損）
            - total_equity: float
                帳戶總權益
        - Return:
            - bool
                是否觸發降級
        """

        cap: float = total_equity * self.config.account_daily_loss_ratio
        if loss <= cap:
            return False

        reason: str = f"帳戶當日虧損 {loss:,.0f} 超過上限 {cap:,.0f}"
        self.mode_state.degrade(TradingMode.REDUCE_ONLY, reason)
        self._write_event("ACCOUNT_DAILY_LOSS", NotifyLevel.CRITICAL, reason, None)
        return True

    def on_degrade_event(
        self,
        reason: str,
        target: TradingMode = TradingMode.REDUCE_ONLY,
        strategy_name: Optional[str] = None,
    ) -> None:
        """
        - Description:
            接收其他元件送來的降級事件

            **對帳差異一律走帳戶層**（`strategy_name=None`）：差異無法歸因到
            單支策略，猜錯的代價是讓真正有問題的那支繼續交易。
        - Parameters:
            - reason: str
                原因
            - target: TradingMode
                目標模式
            - strategy_name: Optional[str]
                指定時只降該策略
        """

        self.mode_state.degrade(target, reason, strategy_name=strategy_name)
        self._write_event("DEGRADE", NotifyLevel.CRITICAL, reason, strategy_name)

    # === kill switch ===
    def is_kill_switch_on(self) -> bool:
        """
        - Description:
            kill switch 是否生效

            **每張單送出前都檢查**。只在啟動時檢查等於沒有 kill switch——
            真正需要它的時候，程式早就已經在跑了。
        - Return:
            - bool
                檔案是否存在
        """

        try:
            return self.kill_switch_path.exists()
        except OSError as exc:
            # 檔案系統查不了時**當成生效**：這是安全開關，不確定就停。
            # 反過來（當成沒生效）會在最需要它的時候放行
            logger.error(f"無法檢查 kill switch（視為生效）：{exc}")
            return True

    # === 紀錄 ===
    def record_rejection(
        self, order: BaseOrder, strategy_name: str, decision: RiskDecision
    ) -> None:
        """把拒單寫進 `live_risk_event`；內容包含訂單摘要與觸發的規則"""

        logger.warning(
            f"風控拒單（{decision.category}）：{strategy_name} "
            f"{order.symbol} {order.action.value} {order.volume} — {decision.reason}"
        )
        self._write_event(
            decision.category,
            NotifyLevel.WARN,
            decision.reason,
            strategy_name,
            symbol=order.symbol,
        )

    def _write_event(
        self,
        category: str,
        severity: NotifyLevel,
        message: str,
        strategy_name: Optional[str],
        symbol: Optional[str] = None,
    ) -> None:
        """寫一筆風控事件；沒有 DAO 時只留 log（測試用）"""

        if self.dao is None:
            return

        self.events.write(
            category=category,
            severity=severity,
            message=message,
            strategy_name=strategy_name,
            symbol=symbol,
        )
