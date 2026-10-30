import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from core.broker.base import BaseBroker
from core.live.capital_allocator import CapitalAllocator
from core.live.clock import SegmentClock
from core.live.context import StrategyContext
from core.live.risk.risk_manager import PreTradeRiskManager
from core.models import BaseAccount, BaseOrder, BrokerAccountSnapshot
from core.portfolio import order_rules
from core.utils import LiveOrderStatus

"""
AccountMonitor：權益、資金刷新與單日虧損檢查

策略可用的資金基準只有一個口徑（`account_equity()`）：額度檢查、單日虧損與
批次曝險都拿它當分母，各處自己內嵌一份公式的話，改了一邊另一邊不會跟著改。
正式環境取券商快照的總權益；模擬環境的帳務是假的，一律以 Σ 宣告額度為虛擬資金。
"""


class AccountMonitor:
    """帳戶層的錢：總權益口徑、額度刷新、本地餘額重設、單日虧損檢查"""

    def __init__(
        self,
        contexts: Sequence[StrategyContext],
        broker: BaseBroker,
        order_manager: Any,
        allocator: CapitalAllocator,
        risk_manager: PreTradeRiskManager,
        clock: SegmentClock,
        fetch_account: Optional[Callable[[], BrokerAccountSnapshot]] = None,
        simulation: bool = False,
    ) -> None:
        """
        - Description:
            建立帳務監看；相依一律由外部注入
        - Parameters:
            - contexts: Sequence[StrategyContext]
                各策略的執行脈絡
            - broker: BaseBroker
                券商閘道（預設帳務查詢、當日已實現損益）
            - order_manager: Any
                委託管理（當日已送出的開倉單）
            - allocator: CapitalAllocator
                資金額度分配
            - risk_manager: PreTradeRiskManager
                事前風控（單日累計起算、虧損降級）
            - clock: SegmentClock
                段落時鐘
            - fetch_account: Optional[Callable[[], BrokerAccountSnapshot]]
                取帳務快照的方式；`None` 時退回 `broker.get_account()`。
                **由組裝層注入**：股票與期貨是兩個子帳戶，「這一批策略該看哪一筆」
                是商品語意，本模組不出現任何商品字樣
            - simulation: bool
                是否連模擬環境。**預設 False（正式環境的嚴格行為）**
        """

        self.contexts: Sequence[StrategyContext] = contexts
        self.broker: BaseBroker = broker
        self.order_manager: Any = order_manager
        self.allocator: CapitalAllocator = allocator
        self.risk_manager: PreTradeRiskManager = risk_manager
        self.clock: SegmentClock = clock
        self.fetch_account: Optional[Callable[[], BrokerAccountSnapshot]] = (
            fetch_account
        )
        self.simulation: bool = simulation
        # 最近一次刷新的券商帳務快照；`account_equity()` 由它取總權益。
        # **不存進 `CapitalAllocator`**：總權益是券商事實，而 allocator 管的是
        # 額度與保留，兩件事放一起會讓「這個數字是誰說的」失去單一出處
        self.account_snapshot: Optional[BrokerAccountSnapshot] = None

    def account_equity(self) -> float:
        """
        - Description:
            策略可用的資金基準；額度檢查、單日虧損與批次曝險共用這一個口徑

            **正式環境：取券商快照的 `total_equity`**，而組裝層注入的帳務查詢已把
            **不屬於任何策略的部位（`__unattributed__`）扣掉**（見 `factory.make_account_fetcher()`）。
            那些部位是接管來的舊持股，策略不能拿它們當額度——算進分母的話，
            一個沒有現金、只有舊持股的帳戶也會通過額度檢查。未交割款仍算在內：
            它是 T+2 後就會入帳的錢，拿掉會讓隔日還有部位時必然誤判成額度超標。

            **模擬環境：一律以 Σ 宣告額度為虛擬資金**（見 `uses_virtual_capital()`）。

            放寬只記 `logger.warning`，不落地成風控事件：這條路每一次模擬都會走，
            每輪都寫一筆 CRITICAL 等於沒有 CRITICAL。
        - Return:
            - float
                資金基準
        """

        if self.uses_virtual_capital():
            return sum(self.allocator.quotas.values())

        return (
            self.account_snapshot.total_equity
            if self.account_snapshot is not None
            else 0.0
        )

    def uses_virtual_capital(self) -> bool:
        """
        - Description:
            是否以宣告額度當作虛擬資金（模擬環境一律是）

            **模擬環境的帳務是假的**（Shioaji 文件〈模擬模式注意事項〉）：`account_balance()`
            一律回 0、交割款與額度查詢回空值，只有持倉與損益走模擬伺服器；
            模擬下單也**不檢查資金**（模擬環境的拒單訊息沒有任何一條是資金不足）。
            照實拿券商的 0 當可用資金的話，額度分配會把每一張買單都擋下來，
            演練永遠驗不到下單路徑；拿模擬持倉的市值當權益，又會把紙上部位當成真錢。
            **正式環境永遠回 False**：那裡的數字就是真的。
        - Return:
            - bool
                模擬環境為 True
        """

        return self.simulation

    def preload_daily_amounts(self, today: datetime.date) -> None:
        """
        以紀錄庫裡當日已送出的開倉單金額起算風控的單日累計

        三個段落是三個行程、重啟也歸零，單日累計上限要防的正是「重啟反覆送單」。
        被拒與送單失敗的不算（沒有送出去）；出場單不算（上限只管開倉）。
        還原不出訂單的列算不出金額，記 warning 略過——寧可少算也不猜。
        """

        by_name: Dict[str, StrategyContext] = {
            context.name: context for context in self.contexts
        }
        totals: Dict[str, float] = {name: 0.0 for name in by_name}
        for row in self.order_manager.orders_of_day(today):
            context: Optional[StrategyContext] = by_name.get(
                str(row.get("strategy_name"))
            )
            if context is None or str(row.get("status")) in (
                LiveOrderStatus.REJECTED.value,
                LiveOrderStatus.FAILED.value,
            ):
                continue
            order: Optional[BaseOrder] = self.order_manager.rebuild_order(row)
            if order is None:
                logger.warning(
                    f"委託 {row.get('client_order_id')} 還原不出訂單，當日累計金額少算這一筆"
                )
                continue
            if order_rules.is_closing(order):
                continue
            totals[context.name] += context.notional(order)

        for name, amount in totals.items():
            self.risk_manager.preload_daily_amount(name, amount)
            if amount > 0:
                logger.info(
                    f"{name} 本日啟動前已送出開倉金額 {amount:,.0f}，累計上限由此起算"
                )

    def reset_local_balances(self) -> None:
        """
        - Description:
            啟動重建後，把各策略本地帳的餘額設為「額度 − 還持有部位的金額」

            重建時為了把部位還原進帳戶，會暫時停用餘額檢查、結束後把餘額設回原值，
            而原值是建帳時的完整額度——**持有部位的成本因此沒有被扣掉**。
            部位管理層會照這個偏大的餘額算張數，再被額度分配整筆拒絕。
            額度本身固定不滾入已實現損益，與額度分配的 `額度 − 已用` 同一個口徑。
        """

        for context in self.contexts:
            quota: float = self.allocator.quotas.get(
                context.name, context.account.balance
            )
            context.account.balance = max(quota - context.position_value(), 0.0)

    def check_daily_loss(self) -> None:
        """
        段落開始前的虧損檢查：逐策略降級 ＋ 帳戶層降級

        **放開盤前不放段落結束**：開盤前就發現昨天虧太多，這個段落直接不送新倉單；
        放在結束才判等於本段落已經白送一輪。

        **兩層的損益來源不同，狀態也不同**：

        - **帳戶層：取券商端**（已實現 ＋ 未實現）。資料不足時**略過並記 warning**，
          不當成「沒有虧損」。
        - **策略層：目前恆為不觸發，缺的是輸入不是接線。** `prepare()` 走到這裡時，
          帳戶是剛由 `AccountSynchronizer._restore_positions()` 以**原始開倉價**重建的，
          未實現損益因此是 0；而 `OrderManager.recover()` 只接管未終結的委託、
          不回放已成交的回報，本段落之前的已實現損益也不在這個行程的 `trade_records` 裡。

        **策略層刻意留著接線而不是拿掉**：判定與降級的路徑本身是對的，缺的只是損益來源。
        券商只給得出逐標的的合併損益，拆不回策略——要逐策略判定得以歸屬帳的逐策略
        均價自行推算，那是另一步。
        """

        for context in self.contexts:
            self.risk_manager.check_daily_loss(
                context.name,
                self._strategy_loss(context),
                context.capital_limit,
                context.risk_limits,
            )

        account_loss: Optional[float] = self.account_loss()
        if account_loss is None:
            logger.warning(
                "取不到券商端的當日損益，本段落略過帳戶層虧損檢查"
                "（**不是判定為沒有虧損**）"
            )
            return

        self.risk_manager.check_account_daily_loss(account_loss, self.account_equity())

    def account_loss(self) -> Optional[float]:
        """
        - Description:
            帳戶當日虧損金額（正數表示虧損）；取不到時回 `None`

            **取券商端而不是各策略本地帳的加總**：日頻模式下三個段落是三個獨立
            行程，本地帳每次都由原始開倉價重建，未實現恆為 0、當日已實現也不在
            這個行程裡——加總出來永遠是 0，那道守門等於不存在。

            **取不到一律回 `None`，不可回 0**：0 的語意是「沒有虧損」，
            回 0 會讓這道檢查看起來有在跑、實際永遠不觸發。
            回 `None` 讓呼叫端明確略過並留下紀錄。

            **已實現與未實現分兩個來源**：未實現在帳務快照上（由持倉逐檔加總），
            當日已實現要另外查券商的已平倉交易——快照的 `realized_pnl`
            目前沒有任何券商實作會填，讀它只會拿到 0。
        - Return:
            - Optional[float]
                虧損金額（正數為虧損）；資料不足時為 None
        """

        if self.account_snapshot is None:
            return None

        realized: Optional[float] = self._today_realized_pnl()
        if realized is None:
            return None

        return -(realized + self.account_snapshot.unrealized_pnl)

    def _today_realized_pnl(self) -> Optional[float]:
        """
        當日已實現損益；閘道不提供查詢時回 `None`

        **不把「查不到」當成 0**：那會讓帳戶層的虧損被低估成只剩未實現的部分，
        而當日沖銷完的虧損正好一毛都不算。
        """

        provider: Optional[Callable[..., Any]] = getattr(
            self.broker, "get_realized_trades", None
        )
        if provider is None:
            return None

        try:
            trades: List[Any] = list(provider(self.clock.now().date()))
        except Exception as exc:
            # 帳務查詢失敗不該讓段落起不來；但也不可當成「今天沒有已實現損益」
            logger.opt(exception=True).warning(f"取當日已實現損益失敗：{exc}")
            return None

        return float(sum(trade.pnl for trade in trades))

    @staticmethod
    def _strategy_loss(context: StrategyContext) -> float:
        """
        本策略目前的虧損金額（正數表示虧損）：已實現 ＋ 未實現

        **一律取本地帳**（與回測同一套算法），不取券商快照：對帳不一致本身
        會另外觸發降級，兩個來源在那個時候會給出不同的答案。
        """

        account: BaseAccount = context.account
        account.update_realized_pnl()
        unrealized: float = sum(
            position.unrealized_pnl
            for position in account.positions
            if not position.is_closed
        )
        return -(account.realized_pnl + unrealized)

    def refresh_capital(self) -> None:
        """刷新帳戶可用餘額與各策略的持倉占用；段落內不再逐單查帳務"""

        snapshot: BrokerAccountSnapshot = self.query_account_snapshot()
        used: Dict[str, float] = {
            context.name: context.position_value() for context in self.contexts
        }
        self.account_snapshot = snapshot
        available: float = (
            # 模擬環境的可用現金恆為 0（見 `uses_virtual_capital()`），改以額度扣掉已用
            max(sum(self.allocator.quotas.values()) - sum(used.values()), 0.0)
            if self.uses_virtual_capital()
            else snapshot.available_balance
        )
        self.allocator.refresh(available, used)

    def query_account_snapshot(self) -> BrokerAccountSnapshot:
        """
        - Description:
            取得本次要當作基準的帳務快照

            **查哪個帳戶由組裝層決定、以 `fetch_account` 注入**：股票與期貨是
            兩個子帳戶，各有各的錢，而「這一批策略該看哪一筆」是商品語意——
            本檔刻意不出現任何商品字樣（見模組說明）。

            未注入時退回券商的預設帳務查詢，維持既有呼叫端的行為。
        - Return:
            - BrokerAccountSnapshot
                帳務快照
        """

        if self.fetch_account is not None:
            return self.fetch_account()
        return self.broker.get_account()
