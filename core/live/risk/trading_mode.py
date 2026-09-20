import datetime
from enum import Enum
from typing import Callable, Dict, Optional

from loguru import logger

from core.config.settings import now_live
from core.dao.tw.live_trade_dao import LiveTradeDAO

"""
TradingMode：交易模式狀態機

三個值、一條方向：

| 模式 | 允許 | 用在 |
|------|------|------|
| `NORMAL` | 全部 | 正常 |
| `REDUCE_ONLY` | 只允許平倉 | 行情中斷、報價過期、當日虧損上限、對帳不一致 |
| `HALTED` | 全停（連平倉都不送） | kill switch、部位本身不可信 |

`HALTED` 連平倉都不送，代表部位會失去停損能力，所以**只用在「連部位都不可信」
的情況**。行情中斷這類只降到 `REDUCE_ONLY`——那時部位還在場上，
降到 `HALTED` 會連停損都送不出去。

**由風控單一持有。** 對帳器、盤中事件迴圈、券商 session 都不自己改模式，
只送降級事件過來。散在各元件各自切換的話，沒有任何一處知道「現在到底能不能送單」。

**只能單向降級，恢復一律人工。** 會自動降級的條件多半是偵測不完整的異常，
自動恢復等於賭它已經好了。
"""

# 交易模式常量
TRADING_MODE_NORMAL = "NORMAL"
TRADING_MODE_REDUCE_ONLY = "REDUCE_ONLY"
TRADING_MODE_HALTED = "HALTED"


class TradingMode(str, Enum):
    """交易模式；值即落地到 `live_run.account_mode` 與 `live_strategy_mode.mode` 的內容"""

    NORMAL = TRADING_MODE_NORMAL
    REDUCE_ONLY = TRADING_MODE_REDUCE_ONLY
    HALTED = TRADING_MODE_HALTED


# 嚴格程度；數字越大越嚴格
_SEVERITY: Dict[TradingMode, int] = {
    TradingMode.NORMAL: 0,
    TradingMode.REDUCE_ONLY: 1,
    TradingMode.HALTED: 2,
}


def stricter(first: TradingMode, second: TradingMode) -> TradingMode:
    """
    - Description:
        取兩個模式中較嚴格的那一個

        多策略下「能不能送單」要同時看帳戶層與策略層，取嚴格者。
        取寬鬆者的話，帳戶層已經 halt 了某支策略還在送單。
    - Parameters:
        - first: TradingMode
            模式一
        - second: TradingMode
            模式二
    - Return:
        - TradingMode
            較嚴格者
    """

    return first if _SEVERITY[first] >= _SEVERITY[second] else second


class TradingModeState:
    """
    - Description:
        交易模式的唯一持有者

        帳戶層一個模式、每支策略各一個；實際能否送單取兩者的嚴格者。
        **兩層都要落地**：日頻的 open／close／after_close 是三個獨立行程，
        策略層若不落地，開盤段因當日虧損上限而降級的那支策略，
        到 13:20 尾盤段重新啟動時會靜默回到 `NORMAL` 繼續開新倉。
    """

    def __init__(
        self,
        dao: Optional[LiveTradeDAO] = None,
        run_id: str = "",
        now_provider: Callable[[], datetime.datetime] = now_live,
    ) -> None:
        """
        - Description:
            建立狀態機（模式皆為 `NORMAL`，要讀回上次狀態請呼叫 `load()`）
        - Parameters:
            - dao: Optional[LiveTradeDAO]
                紀錄庫；None 時不落地（測試用）
            - run_id: str
                本次啟動的識別碼
            - now_provider: Callable[[], datetime.datetime]
                取得目前時間
        """

        self.dao: Optional[LiveTradeDAO] = dao
        self.run_id: str = run_id
        self._now: Callable[[], datetime.datetime] = now_provider

        self.account_mode: TradingMode = TradingMode.NORMAL
        self.strategy_modes: Dict[str, TradingMode] = {}

    # === 落地與讀回 ===
    def load(self) -> None:
        """
        - Description:
            從紀錄庫讀回上次的模式

            **不讀回就等於偷偷解除 halt**——這是實盤最常見的事故型態：
            程式因對帳不一致停下來，值班的人直接重啟，於是帶著錯誤部位繼續交易。
            日頻下按重啟鍵的還可能是 crontab。
        """

        if self.dao is None:
            return

        self.account_mode = TradingMode(self.dao.get_last_account_mode())
        self.strategy_modes = {
            name: TradingMode(mode)
            for name, mode in self.dao.get_strategy_modes().items()
        }

        if self.account_mode is not TradingMode.NORMAL:
            logger.warning(f"讀回上次的帳戶層交易模式：{self.account_mode.value}")
        for name, mode in self.strategy_modes.items():
            if mode is not TradingMode.NORMAL:
                logger.warning(f"讀回策略 {name} 的交易模式：{mode.value}")

    # === 查詢 ===
    def effective_mode(self, strategy_name: Optional[str] = None) -> TradingMode:
        """
        - Description:
            某支策略實際適用的模式：帳戶層與策略層取嚴格者
        - Parameters:
            - strategy_name: Optional[str]
                策略名；None 時只看帳戶層
        - Return:
            - TradingMode
                實際模式
        """

        if strategy_name is None:
            return self.account_mode
        return stricter(
            self.account_mode,
            self.strategy_modes.get(strategy_name, TradingMode.NORMAL),
        )

    def allows_open(self, strategy_name: Optional[str] = None) -> bool:
        """這支策略現在可不可以開新倉"""

        return self.effective_mode(strategy_name) is TradingMode.NORMAL

    def allows_close(self, strategy_name: Optional[str] = None) -> bool:
        """
        這支策略現在可不可以送平倉單

        `HALTED` 連平倉都不送，所以它只用在「連部位都不可信」的情況。
        """

        return self.effective_mode(strategy_name) is not TradingMode.HALTED

    # === 降級 ===
    def degrade(
        self,
        target: TradingMode,
        reason: str,
        strategy_name: Optional[str] = None,
    ) -> TradingMode:
        """
        - Description:
            降級到指定模式；**已經比它嚴格時不動**

            升級（放寬）一律不接受——那是 `resume()` 的事，而且只能人工觸發。
        - Parameters:
            - target: TradingMode
                目標模式
            - reason: str
                原因（寫進 `live_risk_event` 與模式表）
            - strategy_name: Optional[str]
                指定時只降該策略；None 時降帳戶層（**全體策略受影響**）
        - Return:
            - TradingMode
                降級後的實際模式
        """

        current: TradingMode = (
            self.account_mode
            if strategy_name is None
            else self.strategy_modes.get(strategy_name, TradingMode.NORMAL)
        )
        new_mode: TradingMode = stricter(current, target)
        if new_mode is current:
            return current

        scope: str = "帳戶層" if strategy_name is None else f"策略 {strategy_name}"
        logger.error(
            f"{scope}交易模式降級：{current.value} → {new_mode.value}（{reason}）"
        )

        if strategy_name is None:
            self.account_mode = new_mode
        else:
            self.strategy_modes[strategy_name] = new_mode

        self._persist(new_mode, reason, strategy_name)
        return new_mode

    def resume(self, strategy_name: Optional[str] = None) -> None:
        """
        - Description:
            人工恢復到 `NORMAL`

            **只能由命令列旗標觸發**，程式不自動呼叫它：會自動降級的條件
            多半是偵測不完整的異常，自動恢復等於賭它已經好了。
        - Parameters:
            - strategy_name: Optional[str]
                指定時只恢復該策略；None 時恢復帳戶層
        """

        scope: str = "帳戶層" if strategy_name is None else f"策略 {strategy_name}"
        logger.warning(f"{scope}交易模式由人工恢復為 NORMAL")

        if strategy_name is None:
            self.account_mode = TradingMode.NORMAL
        else:
            self.strategy_modes[strategy_name] = TradingMode.NORMAL

        self._persist(TradingMode.NORMAL, "人工恢復", strategy_name)

    def _persist(
        self, mode: TradingMode, reason: str, strategy_name: Optional[str]
    ) -> None:
        """
        落地模式

        帳戶層在 `live_run` 上（由 `finish_run()` 寫入結束時的值），
        策略層每次變動就寫 `live_strategy_mode`——它沒有「結束」這個時點。
        """

        if self.dao is None or strategy_name is None:
            return

        self.dao.upsert_strategy_mode(
            {
                "strategy_name": strategy_name,
                "mode": mode.value,
                "reason": reason,
                "run_id": self.run_id,
                "changed_at": self._now(),
            }
        )
