from typing import Dict, List, Optional, Sequence, Tuple

from core.models import BaseOrder

"""
Portfolio aggregation：把帳戶內多個 sleeve（由單一策略管理的子組合）合成帳戶層

本檔只放**判定**，而且全部是純函式——不碰 broker、不碰 DB、不碰 wall clock。
讀寫與副作用留在 `core/live/`（`capital_allocator.py`、`attribution/conflict_guard.py`）。

**為什麼現在就切**：資金排擠與同標的仲裁是**只存在於實盤**的行為，回測沒有對應物。
日後要補「多策略組合回測」時，回測端得能重用同一份判定，否則組合回測與實盤
又是兩套邏輯——那正是「策略層不分家」要避免的事，只是這次發生在跨策略層。
現在切幾乎免費，等實盤跑起來再切，是把已經在線上的邏輯搬家。
"""


def allocate_capital(
    quotas: Dict[str, float],
    reserved: Dict[str, float],
    used: Dict[str, float],
    available_balance: float,
) -> Dict[str, float]:
    """
    - Description:
        算出各策略此刻的可用資金

        公式：`min(本策略額度 − 本策略已用, 帳戶可用餘額 − 其他策略已保留)`

        兩項缺一不可。只看第一項的話，兩支策略會同時看到「帳戶還有 100 萬」
        而各自下 80 萬，第二張單被券商退；只看第二項的話，某支策略可以把
        整個帳戶的錢都壓進去，`init_capital` 形同虛設。

        結果不會是負數：額度已經超用時回 0，而不是負值——負的「可用資金」
        傳到下游會被當成一個可以下單的數字。

        **不收帳戶總權益與安全係數**：那兩個是啟動時的額度總量檢查要用的，
        與「此刻還能下多少」是兩件事，由 `check_quota_against_equity()` 負責。
        把用不到的參數留在簽章裡，日後會有人以為改它會影響這裡的計算。
    - Parameters:
        - quotas: Dict[str, float]
            各策略的額度上限（`init_capital`）
        - reserved: Dict[str, float]
            各策略目前保留中（已送出、尚未終結）的金額
        - used: Dict[str, float]
            各策略已佔用（持倉）的金額
        - available_balance: float
            帳戶目前可動用的餘額
    - Return:
        - Dict[str, float]
            `{策略: 可用金額}`
    """

    total_reserved: float = sum(reserved.values())
    result: Dict[str, float] = {}

    for name, quota in quotas.items():
        own_used: float = used.get(name, 0.0) + reserved.get(name, 0.0)
        by_quota: float = quota - own_used
        by_account: float = available_balance - (
            total_reserved - reserved.get(name, 0.0)
        )
        result[name] = max(min(by_quota, by_account), 0.0)

    return result


def check_quota_against_equity(
    quotas: Dict[str, float], account_equity: float, safety_ratio: float
) -> Optional[str]:
    """
    - Description:
        啟動時檢查：Σ 各策略額度 ≤ 帳戶總權益 × 安全係數

        **分母是總權益（可用餘額 ＋ 持倉市值），不是可用餘額。**
        拿可用餘額當分母的話，只要隔日還有部位在場上，餘額就已經被部位佔掉，
        檢查必然誤判成額度超標而拒絕啟動——而那是**完全正常的續跑狀態**。
        這個錯在「第一天空手啟動」的測試裡完全看不出來，要到第二天才爆，
        且爆的形式是「今天整天沒跑」。
    - Parameters:
        - quotas: Dict[str, float]
            各策略的額度上限
        - account_equity: float
            帳戶總權益
        - safety_ratio: float
            安全係數
    - Return:
        - Optional[str]
            不通過時回傳說明（含各策略額度與缺口）；通過時為 None
    """

    total: float = sum(quotas.values())
    cap: float = account_equity * safety_ratio
    if total <= cap:
        return None

    breakdown: str = "、".join(
        f"{name}={quota:,.0f}" for name, quota in sorted(quotas.items())
    )
    return (
        f"Σ 各策略額度 {total:,.0f} 超過帳戶總權益 {account_equity:,.0f} × "
        f"{safety_ratio:.0%} = {cap:,.0f}，缺口 {total - cap:,.0f}（{breakdown}）"
    )


def resolve_symbol_conflicts(
    orders: Sequence[Tuple[str, BaseOrder]],
    holders: Dict[str, str],
    is_closing: Dict[int, bool],
) -> Tuple[List[Tuple[str, BaseOrder]], List[Tuple[str, BaseOrder, str]]]:
    """
    - Description:
        同一標的只允許一支策略持有（先搶先贏）

        **平倉單不受限**：擋平倉會讓部位失去出場能力，那比衝突嚴重得多。

        輸入順序即優先順序（呼叫端已依 `sort_orders()` 排好），**不要用策略的
        註冊順序**——那會讓結果取決於設定檔的排列。

        持有者表由呼叫端查好再傳進來，本函式不自己查——這樣它才是純函式，
        日後的多策略組合回測可以直接重用。
    - Parameters:
        - orders: Sequence[Tuple[str, BaseOrder]]
            `[(策略名, 訂單)]`，順序即優先順序
        - holders: Dict[str, str]
            `{symbol: 目前持有的策略}`；未歸屬部位的持有者是保留策略名，**不是缺席**
        - is_closing: Dict[int, bool]
            `{orders 的索引: 是否為平倉單}`
    - Return:
        - Tuple[List[Tuple[str, BaseOrder]], List[Tuple[str, BaseOrder, str]]]
            （放行清單, [(策略名, 被擋的單, 原因)]）
    """

    claimed: Dict[str, str] = dict(holders)
    allowed: List[Tuple[str, BaseOrder]] = []
    blocked: List[Tuple[str, BaseOrder, str]] = []

    for index, (strategy_name, order) in enumerate(orders):
        if is_closing.get(index, False):
            allowed.append((strategy_name, order))
            continue

        holder: Optional[str] = claimed.get(order.symbol)
        if holder is not None and holder != strategy_name:
            blocked.append(
                (
                    strategy_name,
                    order,
                    f"{order.symbol} 已由 {holder} 持有或掛單，"
                    f"{strategy_name} 的新倉單被跨策略守門擋下",
                )
            )
            continue

        claimed[order.symbol] = strategy_name
        allowed.append((strategy_name, order))

    return (allowed, blocked)
