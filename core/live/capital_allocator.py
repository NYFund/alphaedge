import datetime
from typing import Callable, Dict, Optional

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.notify.base import NotifyLevel
from core.live.risk.event_log import RiskEventLogger
from core.live.risk.risk_config import CAPITAL_SAFETY_RATIO
from core.portfolio.aggregation import allocate_capital, check_quota_against_equity

"""
CapitalAllocator：多策略共用一個餘額時的額度保留與釋放

沒有它的話，兩支策略會同時看到「帳戶還有 100 萬」而各自下 80 萬，
第二張單被券商退——而那時第一張已經成交了。

**判定在 `core/portfolio/aggregation.py`**（純函式），這裡只負責讀帳務、
持有保留狀態、寫風控事件。分開是為了讓日後的多策略組合回測能重用同一份判定。

**釋放一律走 `try/finally`。** 漏釋放會讓額度單向消耗到策略再也送不出單，
而且不會有任何錯誤——`available()` 只會愈算愈小。
"""


class CapitalAllocator:
    """
    - Description:
        資金額度分配

        一個帳戶一個 allocator（與 `OrderManager`、`RateLimiter` 同理）：
        分成多個實例時，每個都以為自己還有額度。
    """

    def __init__(
        self,
        quotas: Dict[str, float],
        dao: Optional[LiveTradeDAO] = None,
        run_id: str = "",
        safety_ratio: float = CAPITAL_SAFETY_RATIO,
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立分配器
        - Parameters:
            - quotas: Dict[str, float]
                各策略的額度上限（`init_capital`）
            - dao: Optional[LiveTradeDAO]
                紀錄庫；額度不足的拒單寫進 `live_risk_event`
            - run_id: str
                本次啟動的識別碼
            - safety_ratio: float
                安全係數
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.quotas: Dict[str, float] = dict(quotas)
        self.dao: Optional[LiveTradeDAO] = dao
        self.run_id: str = run_id
        self.safety_ratio: float = safety_ratio
        self._now: Callable[[], datetime.datetime] = now_provider
        # 風控事件的唯一寫入口。**自建而不是注入**：本類已經持有
        # `(dao, run_id, now_provider)` 三件組，注入只是把同一組東西再傳一次，
        # 卻要改動建構子簽名與每一個建這個類別的地方
        self.events: RiskEventLogger = RiskEventLogger(
            self.dao, self.run_id, now_provider=self._now
        )

        # 保留中（已送出、尚未終結）與已佔用（持倉）的金額
        self.reserved: Dict[str, float] = {name: 0.0 for name in quotas}
        self.used: Dict[str, float] = {name: 0.0 for name in quotas}

        # 帳戶可用餘額，每個段落開始時刷新一次。
        # **段落內不逐單查**：帳務類限流只有 25 次／5 秒，逐單查會在尾盤段把額度吃光
        self.available_balance: float = 0.0

    # === 啟動檢查 ===
    def verify_quota(self, account_equity: float) -> None:
        """
        - Description:
            啟動時檢查額度總量；不滿足就**拒絕啟動**

            等到盤中被券商退單才發現，那時已經有部位在場上。
        - Parameters:
            - account_equity: float
                帳戶總權益（可用餘額 ＋ 持倉市值）
        - Raise:
            - ValueError
                Σ 各策略額度超過帳戶總權益 × 安全係數
        """

        problem: Optional[str] = check_quota_against_equity(
            self.quotas, account_equity, self.safety_ratio
        )
        if problem is not None:
            raise ValueError(problem)

        logger.info(
            f"資金額度檢查通過：Σ 額度 {sum(self.quotas.values()):,.0f} ≤ "
            f"總權益 {account_equity:,.0f} × {self.safety_ratio:.0%}"
        )

    # === 帳務刷新 ===
    def refresh(self, available_balance: float, used: Dict[str, float]) -> None:
        """
        - Description:
            段落開始時刷新帳戶可用餘額與各策略的持倉占用
        - Parameters:
            - available_balance: float
                帳戶可動用餘額
            - used: Dict[str, float]
                各策略的持倉占用金額
        """

        self.available_balance = available_balance
        self.used = {name: used.get(name, 0.0) for name in self.quotas}

    # === 保留與釋放 ===
    def available(self, strategy_name: str) -> float:
        """
        - Description:
            這支策略此刻還能動用多少
        - Parameters:
            - strategy_name: str
                策略名
        - Return:
            - float
                可用金額
        """

        return allocate_capital(
            self.quotas, self.reserved, self.used, self.available_balance
        ).get(strategy_name, 0.0)

    def reserve(self, strategy_name: str, amount: float) -> bool:
        """
        - Description:
            送單前保留金額；不足時回 False 並寫風控事件

            **回 False 而不是拋出**：資金不足是預期內的狀況（多策略搶同一筆餘額），
            不是錯誤。拋出會讓整批委託停在這裡，包括後面那些金額較小、
            其實送得出去的單。
        - Parameters:
            - strategy_name: str
                策略名
            - amount: float
                要保留的金額
        - Return:
            - bool
                是否保留成功
        """

        if strategy_name not in self.quotas:
            raise KeyError(f"策略 {strategy_name} 沒有登記資金額度")

        if amount > self.available(strategy_name):
            reason: str = (
                f"{strategy_name} 可用資金 {self.available(strategy_name):,.0f} "
                f"不足以保留 {amount:,.0f}"
            )
            logger.warning(reason)
            self._write_event("CAPITAL_EXHAUSTED", reason, strategy_name)
            return False

        self.reserved[strategy_name] += amount
        return True

    def release(self, strategy_name: str, amount: float) -> None:
        """
        - Description:
            釋放保留（成交、拒單、撤單、逾時各自呼叫一次）

            **一律走 `try/finally`**：漏釋放會讓額度單向消耗到策略再也送不出單，
            而且不會有任何錯誤——`available()` 只會愈算愈小。

            釋放量大於保留量時夾在 0：負的保留會讓其他策略看到憑空多出來的錢。
        - Parameters:
            - strategy_name: str
                策略名
            - amount: float
                要釋放的金額
        """

        if strategy_name not in self.reserved:
            return

        self.reserved[strategy_name] = max(self.reserved[strategy_name] - amount, 0.0)

    def release_all(self, strategy_name: str) -> float:
        """
        - Description:
            釋放某支策略所有未終結的保留

            **策略層降級時一定要呼叫它**。只停掉送單而不回收保留的話，
            該策略的額度會被佔住到重啟為止——而 `available()` 的第二項是
            「帳戶可用餘額 − 其他策略已保留」，所以被佔住的是**其他策略**的
            可用資金，症狀是別的策略莫名其妙送不出單。
        - Parameters:
            - strategy_name: str
                策略名
        - Return:
            - float
                實際釋放的金額
        """

        released: float = self.reserved.get(strategy_name, 0.0)
        if released > 0:
            logger.warning(
                f"{strategy_name} 降級，釋放其未終結的資金保留 {released:,.0f}"
            )
        self.reserved[strategy_name] = 0.0
        return released

    def _write_event(self, category: str, message: str, strategy_name: str) -> None:
        """寫一筆風控事件；沒有 DAO 時只留 log"""

        self.events.write(
            category=category,
            severity=NotifyLevel.WARN,
            message=message,
            strategy_name=strategy_name,
        )
