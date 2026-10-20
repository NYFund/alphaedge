import datetime
from typing import List

from strategies.stock.investment_trust_momentum_swing_strategy import (
    InvestmentTrustMomentumSwingStrategy,
)

"""
投信波段策略的持有天數：清單查不到時回頭問資料庫，不以曆日近似

實盤的報價日會超出回測期間預先取好的交易日清單。以曆日除以固定倍數近似的話，
跨春節（中間休市一週以上）時算出來的天數比交易日多好幾天，實盤提早出場、回測卻續抱。
"""

# 2027 年初的假交易日曆：02-05～02-14 休市（春節），02-17 是「今天」，日 K 尚未入庫
_CALENDAR: List[datetime.date] = [
    datetime.date(2027, 2, day) for day in (1, 2, 3, 4, 15, 16, 17, 18, 19, 22)
]
_OPEN_DATE: datetime.date = datetime.date(2027, 2, 1)
_TODAY: datetime.date = datetime.date(2027, 2, 17)


class FakePriceAPI:
    """只提供交易日查詢；`as_of` 之後（含）的日 K 視為尚未入庫"""

    def __init__(self, as_of: datetime.date) -> None:
        self.as_of: datetime.date = as_of
        self.queries: int = 0

    def get_trading_days(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[datetime.date]:
        self.queries += 1
        return [
            day
            for day in _CALENDAR
            if start_date <= day <= end_date and day < self.as_of
        ]


def _strategy(
    trading_days: List[datetime.date],
) -> InvestmentTrustMomentumSwingStrategy:
    strategy: InvestmentTrustMomentumSwingStrategy = (
        InvestmentTrustMomentumSwingStrategy()
    )
    strategy.price = FakePriceAPI(as_of=_TODAY)
    strategy.trading_days = trading_days
    return strategy


def test_out_of_range_dates_count_trading_days_across_a_long_holiday() -> None:
    """
    清單只到 02-04、報價日在春節之後：以資料庫的交易日計，當天另計 1 天

    02-01 開倉到 02-17 是 6 個交易日（02、03、04、15、16 加當天），未滿 10 天不出場；
    曆日 16 天除以 1.4 會算成 11 天而提早出場。
    """

    strategy: InvestmentTrustMomentumSwingStrategy = _strategy(_CALENDAR[:4])

    assert strategy.count_holding_days(_OPEN_DATE, _TODAY) == 6
    assert strategy.count_holding_days(_OPEN_DATE, _TODAY) < strategy.HOLDING_DAYS
    assert strategy.price.queries > 0


def test_list_and_database_paths_agree() -> None:
    """日期在清單內時查清單；同一組日期改走資料庫也要得到同一個天數"""

    from_list: InvestmentTrustMomentumSwingStrategy = _strategy(list(_CALENDAR))
    from_database: InvestmentTrustMomentumSwingStrategy = _strategy([])

    for day in _CALENDAR:
        # 資料庫那條路以「今天的日 K 尚未入庫」查詢，與實盤一致
        from_database.price.as_of = day
        assert from_database.count_holding_days(
            _OPEN_DATE, day
        ) == from_list.count_holding_days(_OPEN_DATE, day), day

    assert from_list.price.queries == 0


def test_open_day_counts_as_zero() -> None:
    strategy: InvestmentTrustMomentumSwingStrategy = _strategy([])

    assert strategy.count_holding_days(_OPEN_DATE, _OPEN_DATE) == 0
