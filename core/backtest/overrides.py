import datetime
from dataclasses import dataclass
from typing import Dict, Optional

from core.config import DEFAULT_FUTURES_START_DATE, DEFAULT_PRICE_START_DATE
from core.strategies.base import BaseStrategy
from core.utils import InstrumentType

"""
回測執行參數的覆寫：區間與初始資金

策略在 `__init__` 宣告預設的回測區間與資金；要換區間或資金（參數掃描、
walk-forward、同一支策略比較不同區間）時，由呼叫端傳入 `BacktestOverrides`，
在組裝任何元件之前寫回策略實例——帳戶、資料源、`Backtester` 與策略自己的
資料預載都讀策略上的這三個值，所以只能在最前面改一次，不能各自覆寫。
"""

# 各商品類別的歷史資料起點；更早的區間查不到任何行情，回測會整段空跑
_DATA_START_DATES: Dict[InstrumentType, datetime.date] = {
    InstrumentType.STOCK: DEFAULT_PRICE_START_DATE,
    InstrumentType.FUTURE: DEFAULT_FUTURES_START_DATE,
}


class InvalidBacktestOverridesError(ValueError):
    """覆寫值不合法：起日晚於迄日、早於資料起點，或資金不為正"""


@dataclass(frozen=True)
class BacktestOverrides:
    """回測區間與初始資金的覆寫值；欄位為 None 表示沿用策略的預設"""

    start: Optional[datetime.date] = None
    end: Optional[datetime.date] = None
    capital: Optional[float] = None

    def apply_to(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            驗證後把覆寫值寫回策略實例

            **先算出最終值再一起驗證**：只給 `--start` 時，要拿它和策略預設的迄日比；
            逐欄驗證會漏掉「新起日晚於舊迄日」這種組合。驗證不過時策略維持原狀。
        - Parameters:
            - strategy: BaseStrategy
                要覆寫的策略實例；就地修改
        - Raise:
            - InvalidBacktestOverridesError
                覆寫後的值不合法
        """

        start: Optional[datetime.date] = (
            self.start if self.start is not None else strategy.start_date
        )
        end: Optional[datetime.date] = (
            self.end if self.end is not None else strategy.end_date
        )

        if start is not None and end is not None and start > end:
            raise InvalidBacktestOverridesError(f"回測起日 {start} 晚於迄日 {end}")

        data_start: Optional[datetime.date] = _DATA_START_DATES.get(
            strategy.instrument_type
        )
        if start is not None and data_start is not None and start < data_start:
            raise InvalidBacktestOverridesError(
                f"回測起日 {start} 早於 {strategy.instrument_type.value} 歷史資料起點 {data_start}"
            )

        if self.capital is not None and self.capital <= 0:
            raise InvalidBacktestOverridesError(f"初始資金必須為正數：{self.capital}")

        strategy.start_date = start
        strategy.end_date = end
        if self.capital is not None:
            strategy.init_capital = self.capital
