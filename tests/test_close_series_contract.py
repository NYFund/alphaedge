import datetime

import pandas as pd

from core.api.base import BaseDataAPI

"""
價格序列的回傳契約：index 為 `datetime.date`、dtype float、name 為「收盤價」、依日期排序

三支價格 API（台股、期貨單一合約、期貨連續合約）原本各回一種形狀，呼叫端只能各自補轉換；
現在都經 `BaseDataAPI.build_close_series()`，契約只在這裡釘一次。各 API 的測試另外確認
它們真的走這一條（見 `tests/test_stock_data_api.py`、`tests/test_futures_price_api.py`、
`tests/backtest/test_futures_benchmark_series.py`）。
"""


def _frame(dates, closes) -> pd.DataFrame:
    """以資料表的形狀（日期欄 ＋ 收盤欄）造一張查詢結果"""

    return pd.DataFrame({"date": dates, "收盤價": closes})


def test_index_is_date_objects_sorted_and_named() -> None:
    """字串日期轉成 `datetime.date`（不是 Timestamp），依日期排序，index 與序列都有名字"""

    series: pd.Series = BaseDataAPI.build_close_series(
        _frame(["2024-01-03", "2024-01-02"], [600, 590]), "date", "收盤價"
    )

    assert list(series.index) == [datetime.date(2024, 1, 2), datetime.date(2024, 1, 3)]
    assert all(type(value) is datetime.date for value in series.index)
    assert list(series) == [590.0, 600.0]
    assert series.name == "收盤價"
    assert series.index.name == "date"
    assert series.dtype == float


def test_series_name_is_the_contract_not_the_column_name() -> None:
    """序列的 name 固定為「收盤價」，不隨來源欄名變動；呼叫端靠這個名字辨認序列"""

    series: pd.Series = BaseDataAPI.build_close_series(
        pd.DataFrame({"date": ["2024-01-02"], "close": [100.0]}), "date", "close"
    )

    assert series.name == "收盤價"


def test_datetime_column_is_accepted_too() -> None:
    """日期欄已是 datetime（例如衍生表）時同樣轉成 `datetime.date`"""

    series: pd.Series = BaseDataAPI.build_close_series(
        _frame(pd.to_datetime(["2024-01-02"]), [100.5]), "date", "收盤價"
    )

    assert list(series.index) == [datetime.date(2024, 1, 2)]


def test_non_numeric_close_becomes_nan_not_error() -> None:
    """無法轉成數值的價格轉成 NaN：缺資料與值異常要分得開，且不可讓整段序列拋錯"""

    series: pd.Series = BaseDataAPI.build_close_series(
        _frame(["2024-01-02", "2024-01-03"], ["--", "600"]), "date", "收盤價"
    )

    assert series.dtype == float
    assert pd.isna(series.iloc[0]) and series.iloc[1] == 600.0


def test_duplicate_dates_are_kept() -> None:
    """同一天多列（期貨日夜盤）維持原樣，不在這裡挑時段"""

    series: pd.Series = BaseDataAPI.build_close_series(
        _frame(["2024-01-02", "2024-01-02"], [100, 101]), "date", "收盤價"
    )

    assert list(series) == [100.0, 101.0]


def test_empty_frame_gives_an_empty_series_of_the_same_shape() -> None:
    """查無資料時 name、dtype 與 index 名稱都和有資料時相同，呼叫端以 `empty` 判斷"""

    for frame in (
        pd.DataFrame(),
        _frame([], []),
        pd.DataFrame({"date": ["2024-01-02"]}),
    ):
        series: pd.Series = BaseDataAPI.build_close_series(frame, "date", "收盤價")

        assert series.empty
        assert series.name == "收盤價"
        assert series.dtype == float
        assert series.index.name == "date"
