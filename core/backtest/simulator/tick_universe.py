import datetime
from typing import List, Optional, Set

from core.models import BaseAccount
from core.strategies.base import BaseStrategy

"""
TICK 回測「策略清單」模式的每日標的：與實盤盤前訂閱同一套規則

實盤盤前以前一交易日呼叫 `get_live_symbols()` 決定要訂閱的標的，再替帳上的持倉補訂閱
（`LiveTrader.quote_symbols()`）；回測照抄這條規則，回測讀到的報價才會和實盤看得到的一樣。
"""


def has_live_symbols(strategy: BaseStrategy) -> bool:
    """策略是否覆寫了 `get_live_symbols()`；沒覆寫就沒有清單可用"""

    return type(strategy).get_live_symbols is not BaseStrategy.get_live_symbols


def resolve_strategy_symbols(
    strategy: BaseStrategy,
    account: BaseAccount,
    previous_trading_day: datetime.date,
) -> List[str]:
    """
    - Description:
        某個交易日盤前的標的：策略以前一交易日給的清單，加上當時持有、卻不在清單裡的標的

        **持倉一定要讀得到報價**：清單每天重新篩選，昨天買的那一檔今天掉出清單的話，
        平倉與停損永遠看不到它，部位就一直留在帳上，而且不會有任何錯誤。
    - Parameters:
        - strategy: BaseStrategy
            策略（必須覆寫 `get_live_symbols()`）
        - account: BaseAccount
            回測帳戶
        - previous_trading_day: datetime.date
            前一交易日（盤前看得到的最新資料日）
    - Return:
        - List[str]
            清單在前、清單外的持倉依代號排序附在後面
    - Raise:
        - ValueError
            策略當天回傳 None：沒有清單就不知道要讀哪些標的，不自動改成全市場
    """

    declared: Optional[List[str]] = strategy.get_live_symbols(previous_trading_day)
    if declared is None:
        raise ValueError(
            f"{type(strategy).__name__}.get_live_symbols({previous_trading_day}) 回傳 None："
            "策略清單模式需要清單；要讀全市場請改用 --tick-universe all"
        )

    held: Set[str] = {position.symbol for position in account.get_positions()} - set(
        declared
    )
    return list(declared) + sorted(held)
