from typing import Any, Callable, Dict, Optional

from loguru import logger

"""
送單前的期貨保證金檢查：策略層與帳戶層兩道

**策略層**以該策略自己的期貨帳戶可動用餘額比對，口徑與回測的
`FuturesPositionManager.open_position()` 相同（原始保證金 ＋ 開倉成本），
模擬環境也有效——回測開不進去的單，實盤在送出前就擋下，而不是成交後帳上開不出部位。

**帳戶層**以券商回報的可用保證金比對：多支策略共用一個期貨帳戶，各自的額度
加起來可能超過帳戶實際有的錢。券商的查詢**在模擬環境一律回 0**
（2026-09-22 實測：所有欄位皆 0、狀態為已取得，但委託照常成交），
因此「全部為 0」視為沒有資料：模擬環境記 warning 略過，**正式環境一律擋單**——
查不到保證金就送單，等於把檢查交給券商退單，而退單訊息看起來像別的問題。

同一批送單內逐張累計：每張都拿同一份可用保證金比，兩張各自過關、合計卻超過。
通過檢查就先計入，之後被其他關卡擋下也不退回——保守的方向，最多少送一張。
"""

# 券商的保證金帳務查詢（台期貨是 `ShioajiBroker.get_futures_account()`）；
# 回傳物件要有 `available_margin` 與 `total_equity`
MarginAccountQuery = Callable[[], Any]


class MarginGate:
    """
    - Description:
        一批送單的保證金預算

        名稱刻意不帶市場：引擎本體（`LiveTrader`）必須市場無關，要用保證金的商品
        由組裝層注入需求計算與券商查詢。每次送單批次建一個新的實例：
        券商的可用保證金只在第一張需要檢查的開倉單時查一次
        （帳務查詢有額度），之後以本批已計入的量扣減。
    """

    def __init__(
        self, account_query: Optional[MarginAccountQuery], simulation: bool
    ) -> None:
        """
        - Description:
            建立保證金預算
        - Parameters:
            - account_query: Optional[MarginAccountQuery]
                券商的保證金帳務查詢；None 代表券商閘道不提供
            - simulation: bool
                是否為模擬環境；決定帳戶層查不到資料時放行還是擋單
        """

        self._query: Optional[MarginAccountQuery] = account_query
        self._simulation: bool = simulation

        self._strategy_used: Dict[str, float] = {}
        self._broker_queried: bool = False
        # None 代表帳戶層沒有可用的資料（且為模擬環境，已決定略過）
        self._broker_left: Optional[float] = None
        self._broker_error: Optional[str] = None

    def check(
        self, strategy_name: str, required: float, strategy_available: float
    ) -> Optional[str]:
        """
        - Description:
            檢查一張開倉單；通過時計入預算
        - Parameters:
            - strategy_name: str
                策略名
            - required: float
                本張需要的資金（原始保證金 ＋ 開倉成本）
            - strategy_available: float
                該策略期貨帳戶的可動用餘額
        - Return:
            - Optional[str]
                擋下的原因；通過時為 None
        """

        used: float = self._strategy_used.get(strategy_name, 0.0)
        if used + required > strategy_available:
            return (
                f"策略可動用餘額不足：需要 {required:,.0f}，"
                f"本批已計入 {used:,.0f}，可動用 {strategy_available:,.0f}"
            )

        self._query_broker_once()
        if self._broker_error is not None:
            return self._broker_error
        if self._broker_left is not None and required > self._broker_left:
            return (
                f"帳戶可用保證金不足：需要 {required:,.0f}，"
                f"剩餘 {self._broker_left:,.0f}"
            )

        self._strategy_used[strategy_name] = used + required
        if self._broker_left is not None:
            self._broker_left -= required
        return None

    def _query_broker_once(self) -> None:
        """第一次需要時才查券商；查不到資料時依環境決定放行或擋單"""

        if self._broker_queried:
            return
        self._broker_queried = True

        if self._query is None:
            self._handle_unavailable("券商閘道不提供保證金查詢")
            return

        try:
            snapshot: Any = self._query()
        except Exception as exc:
            logger.opt(exception=True).warning(f"保證金查詢失敗：{exc}")
            self._handle_unavailable(f"保證金查詢失敗：{exc}")
            return

        available: float = float(getattr(snapshot, "available_margin", 0.0) or 0.0)
        equity: float = float(getattr(snapshot, "total_equity", 0.0) or 0.0)
        if available == 0.0 and equity == 0.0:
            self._handle_unavailable("券商回報的保證金全為 0（沒有資料）")
            return

        self._broker_left = available

    def _handle_unavailable(self, reason: str) -> None:
        """模擬環境略過帳戶層並記 warning；正式環境記下原因，之後每張都擋"""

        if self._simulation:
            logger.warning(f"{reason}；模擬環境略過帳戶層保證金檢查，只做策略層")
            self._broker_left = None
            return
        self._broker_error = f"{reason}；正式環境不送需要保證金的開倉單"
