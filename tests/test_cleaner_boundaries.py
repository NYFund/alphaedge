import datetime
from typing import List

import pandas as pd
import pytest

from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.shared.source_priority import dedup_by_source_priority
from core.pipeline.utils import ColumnLayoutError
from core.pipeline.utils.data_utils import DataUtils

"""
cleaner 的三個邊界，共通點是**出錯時不會有任何錯誤**

1. 無成交日的 `--` 被 `fillna(0)` 填成 0，變成「當天成交價 0 元」。
2. 上櫃依位置命名欄位，版面一改就整批對到錯的名字。
3. 除權息三來源去重依檔名字典序，勝出的是誰取決於字母順序。
"""


# === 無成交價要保持 NULL ===
def test_fill_nan_keeps_price_columns_null() -> None:
    """價格欄維持 NaN，成交量等欄位仍填 0"""

    df: pd.DataFrame = pd.DataFrame(
        {
            "收盤價": [None],
            "開盤價": [None],
            "成交股數": [None],
            "成交筆數": [None],
        }
    )

    result: pd.DataFrame = DataUtils.fill_nan(df, 0, exclude_cols=["收盤價", "開盤價"])

    assert pd.isna(result["收盤價"].iloc[0])
    assert pd.isna(result["開盤價"].iloc[0])
    assert result["成交股數"].iloc[0] == 0
    assert result["成交筆數"].iloc[0] == 0


def test_fill_nan_without_exclusions_keeps_old_behaviour() -> None:
    """沒有指定排除欄位時行為與舊版相同"""

    df: pd.DataFrame = pd.DataFrame({"a": [None], "b": [1.0]})

    result: pd.DataFrame = DataUtils.fill_nan(df, 0)

    assert result["a"].iloc[0] == 0


class _Row:
    """`price` 表一列的最小替身"""

    def __init__(self, stock_id: str, close) -> None:
        self.stock_id: str = stock_id
        self.收盤價 = close
        self.開盤價 = close
        self.最高價 = close
        self.最低價 = close
        self.成交股數 = 1000


@pytest.mark.parametrize("close", [None, float("nan"), 0, 0.0])
def test_adapter_skips_rows_without_a_price(close) -> None:
    """
    無成交價的列不可變成 `StockQuote`

    NULL（cleaner 修好之後）與 0（尚未執行修復腳本的歷史資料）都要濾掉，
    否則策略會拿 0 元價去算報酬、下單、停損。
    """

    quotes = StockQuoteAdapter.from_day_records(
        [_Row("2330", close)], datetime.date(2024, 1, 2)
    )

    assert quotes == []


def test_adapter_keeps_rows_with_a_price() -> None:
    """有成交價的列照常轉換"""

    quotes = StockQuoteAdapter.from_day_records(
        [_Row("2330", 600.0)], datetime.date(2024, 1, 2)
    )

    assert len(quotes) == 1
    assert quotes[0].close == 600.0


# === 依位置命名前要先數欄位 ===
def test_check_column_count_raises_on_mismatch() -> None:
    """欄位數不符要當場拋出，不可繼續依位置命名"""

    df: pd.DataFrame = pd.DataFrame({"a": [1], "b": [2]})

    with pytest.raises(ColumnLayoutError) as exc_info:
        BaseDataCleaner.check_column_count(
            df, expected=3, label="TPEX price 2024-01-02"
        )

    assert exc_info.value.expected == 3
    assert exc_info.value.actual == 2


def test_check_column_count_passes_on_match() -> None:
    """欄位數相符時什麼都不做"""

    df: pd.DataFrame = pd.DataFrame({"a": [1], "b": [2]})

    BaseDataCleaner.check_column_count(df, expected=2, label="TPEX price 2024-01-02")


def test_tpex_price_cleaner_rejects_unexpected_layout() -> None:
    """上櫃版面改了就停下來，而不是把每一欄都對到錯的名字"""

    from core.pipeline.tw.cleaners.stock_price_cleaner import StockPriceCleaner

    # 少一欄（原本應為 15 欄，含 date）
    df: pd.DataFrame = pd.DataFrame(
        [["2330", "台積電"] + [1.0] * 10],
        columns=["代號", "名稱"] + [f"c{i}" for i in range(10)],
    )
    df["發行股數"] = 0
    df["次日漲停價"] = 0
    df["次日跌停價"] = 0

    with pytest.raises(ColumnLayoutError):
        StockPriceCleaner().clean_tpex_price(df, datetime.date(2024, 1, 2))


# === 去重依來源優先序 ===
def make_dividend_rows(sources: List[str]) -> pd.DataFrame:
    """同一個 (date, stock_id) 來自多個來源"""

    return pd.DataFrame(
        [
            {
                "date": "2024-06-20",
                "stock_id": "2330",
                "除權息前收盤價": 100.0 + index,
                "資料來源": source,
            }
            for index, source in enumerate(sources)
        ]
    )


def test_dividend_dedup_prefers_the_highest_priority_source(
    tmp_path, monkeypatch
) -> None:
    """
    勝出的是優先序最高的來源，與出現順序無關

    舊版直接對 `sorted(dir.iterdir())` 的結果 `keep="last"`，
    留下哪一筆取決於檔名的字母順序。
    """

    import core.pipeline.tw.loaders.stock_dividend_loader as loader_module

    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "DIVIDEND_DOWNLOADS_PATH", tmp_path / "dividend")
    loader = loader_module.StockDividendLoader()

    # twse 排在最前面出現，仍應勝出（它的優先序最高）
    result: pd.DataFrame = dedup_by_source_priority(
        make_dividend_rows(["twse", "tpex", "finmind"]),
        loader.SOURCE_PRIORITY,
        label="dividend",
    )

    assert len(result) == 1
    assert result["資料來源"].iloc[0] == "twse"


def test_dividend_dedup_falls_back_when_source_column_missing(
    tmp_path, monkeypatch
) -> None:
    """沒有「資料來源」欄時退回以出現順序去重，但不可整批消失"""

    import core.pipeline.tw.loaders.stock_dividend_loader as loader_module

    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "DIVIDEND_DOWNLOADS_PATH", tmp_path / "dividend")
    loader = loader_module.StockDividendLoader()

    df: pd.DataFrame = make_dividend_rows(["twse", "tpex"]).drop(columns=["資料來源"])
    result: pd.DataFrame = dedup_by_source_priority(
        df, loader.SOURCE_PRIORITY, label="dividend"
    )

    assert len(result) == 1
