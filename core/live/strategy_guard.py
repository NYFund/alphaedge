from typing import Dict, List, Optional, Sequence

from loguru import logger

from core.execution.order_preprocess import get_execution_order
from core.strategies.base import BaseStrategy
from core.utils import BarExecutionOrder, ExecutionTiming, LiveHook

"""
啟動守門：一支策略要上實盤之前必須通過的檢查

擋的都是**不會報錯、只會讓結果安靜地不同**的情況：

1. **沒宣告 `live_ready`** → 策略作者根本沒確認過它的實盤語意。
2. **沒宣告 `live_schedule`** → 引擎不知道哪個鉤子該在哪一段呼叫。
3. **`live_schedule` 與 `bar_execution_order` 矛盾** → 交易順序被默默改掉。

第三條最隱蔽，所以講清楚：回測用 `bar_execution_order` 決定同一根 bar 內
先平後開還是先開後平。實盤拆成開盤段與尾盤段之後，**兩個鉤子分屬不同段落時，
實際順序由段落的先後決定，`bar_execution_order` 形同失效**。
若策略宣告 `CLOSE_THEN_OPEN`，卻把 `close` 排在尾盤段、`open` 排在開盤段，
實際執行就變成先開後平——回測與實盤的部位軌跡從當天起就不同，
而且不會有任何錯誤訊息。
"""


class LiveReadinessError(RuntimeError):
    """策略未通過上實盤前的檢查"""


# 段落的時間先後。`IMMEDIATE` 是盤中逐筆，沒有段落之分，故所有鉤子都在同一格
_TIMING_ORDER: Dict[ExecutionTiming, int] = {
    ExecutionTiming.AT_OPEN: 0,
    ExecutionTiming.AT_CLOSE: 1,
    ExecutionTiming.IMMEDIATE: 0,
}


def verify_strategies(strategies: Sequence[BaseStrategy]) -> None:
    """
    - Description:
        逐支檢查並**一次列出所有問題**

        逐支拋出會讓人修一個、跑一次、再撞下一個。啟動前的檢查要一次講完。
    - Parameters:
        - strategies: Sequence[BaseStrategy]
            本次要上線的策略
    - Raise:
        - LiveReadinessError
            任一支未通過
    """

    problems: List[str] = []
    names: List[str] = []

    for strategy in strategies:
        name: str = type(strategy).__name__
        names.append(name)
        problems.extend(f"[{name}] {issue}" for issue in inspect_strategy(strategy))

    duplicated: List[str] = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        # 歸屬鏈以策略名為鍵（`live_order.strategy_name`、`live_position_lot`），
        # 重名會讓兩支策略的部位與損益混在一起，而合計仍然正確
        problems.append(f"策略名稱重複：{duplicated}；歸屬鏈以策略名為鍵，不可重複")

    if problems:
        raise LiveReadinessError(
            "下列策略未通過上實盤前的檢查：\n  " + "\n  ".join(problems)
        )


def inspect_strategy(strategy: BaseStrategy) -> List[str]:
    """
    - Description:
        檢查單一策略，回傳問題清單（空清單代表通過）
    - Parameters:
        - strategy: BaseStrategy
            待檢查的策略
    - Return:
        - List[str]
            問題說明
    """

    problems: List[str] = []

    if not strategy.live_ready:
        problems.append(
            "未宣告 live_ready=True。這不只是一個旗標：策略的內部狀態必須能由"
            "「歷史資料 ＋ 當前帳戶部位」重建，不可依賴回測逐日跑出來的累積——"
            "實盤會在任意時點重啟，重建不出來的狀態會產生錯訊號且不報錯。"
            "逐項確認後請寫進 class docstring 的〈實盤執行〉區塊"
        )

    if not strategy.live_schedule:
        problems.append(
            "未宣告 live_schedule：引擎不知道哪個鉤子該在哪一段呼叫。"
            "日 K 在實盤不存在，同一支策略的鉤子要拆成開盤段與尾盤段"
        )
    else:
        problems.extend(_check_schedule_keys(strategy))
        conflict: Optional[str] = check_schedule_conflicts(strategy)
        if conflict is not None:
            problems.append(conflict)

    return problems


def _check_schedule_keys(strategy: BaseStrategy) -> List[str]:
    """鍵必須是認得的鉤子名；打錯字會讓那個鉤子整天不被呼叫"""

    known: set = {hook.value for hook in LiveHook}
    unknown: List[str] = sorted(set(strategy.live_schedule) - known)
    if not unknown:
        return []

    return [
        f"live_schedule 有不認得的鉤子名 {unknown}（可用的是 {sorted(known)}）；"
        "打錯字的鉤子整天不會被呼叫，而且不會報錯"
    ]


def check_schedule_conflicts(strategy: BaseStrategy) -> Optional[str]:
    """
    - Description:
        檢查 `live_schedule` 與 `bar_execution_order` 是否矛盾

        判準：
        - 開倉與平倉落在**同一段落**時，順序由 `get_execution_order()` 決定，
          與回測完全一致，不必檢查。
        - 落在**不同段落**時，實際順序由段落先後決定；它必須與
          `bar_execution_order`（策略宣告的，或由方向與當沖旗標推導出來的）一致。

        `ForeignSellShortDayTradeStrategy` 剛好一致（SHORT ＋ 當沖 →
        `OPEN_THEN_CLOSE`，實盤 `open` 在開盤段、`close` 在尾盤段），
        **但那是巧合，不是保證**。
    - Parameters:
        - strategy: BaseStrategy
            待檢查的策略
    - Return:
        - Optional[str]
            矛盾說明；沒有矛盾時為 None
    """

    schedule: Dict[str, ExecutionTiming] = strategy.live_schedule
    open_timing: Optional[ExecutionTiming] = schedule.get(LiveHook.OPEN.value)
    close_timing: Optional[ExecutionTiming] = schedule.get(LiveHook.CLOSE.value)

    if open_timing is None or close_timing is None:
        return None

    if _TIMING_ORDER[open_timing] == _TIMING_ORDER[close_timing]:
        return None

    actual: BarExecutionOrder = (
        BarExecutionOrder.OPEN_THEN_CLOSE
        if _TIMING_ORDER[open_timing] < _TIMING_ORDER[close_timing]
        else BarExecutionOrder.CLOSE_THEN_OPEN
    )
    declared: BarExecutionOrder = get_execution_order(
        strategy.bar_execution_order,
        strategy.position_type,
        strategy.enable_intraday,
    )

    if actual is declared:
        return None

    return (
        f"live_schedule 與 bar_execution_order 矛盾："
        f"open 在 {open_timing.value}、close 在 {close_timing.value}，"
        f"實際順序是 {actual.value}，但回測用的是 {declared.value}。"
        "兩個鉤子分屬不同段落時，實際順序由段落決定，bar_execution_order 形同失效——"
        "宣告相反會**靜默**改掉交易順序，回測與實盤的部位軌跡從當天起就不同"
    )


def resolve_hook_timing(
    strategy: BaseStrategy, hook: LiveHook
) -> Optional[ExecutionTiming]:
    """
    - Description:
        取得某個鉤子的執行段落

        **停損沒宣告時退回平倉的段落**：兩者都是出場，把停損排在與平倉不同的
        段落是刻意的決定，不該由「忘了寫」造成。
    - Parameters:
        - strategy: BaseStrategy
            策略
        - hook: LiveHook
            鉤子
    - Return:
        - Optional[ExecutionTiming]
            執行段落；未宣告且無從退回時為 None
    """

    timing: Optional[ExecutionTiming] = strategy.live_schedule.get(hook.value)
    if timing is not None:
        return timing

    if hook is LiveHook.STOP_LOSS:
        fallback: Optional[ExecutionTiming] = strategy.live_schedule.get(
            LiveHook.CLOSE.value
        )
        if fallback is not None:
            logger.debug(
                f"{type(strategy).__name__} 未宣告 stop_loss 的段落，退回 close 的 "
                f"{fallback.value}"
            )
        return fallback

    return None
