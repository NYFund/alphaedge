import datetime
from pathlib import Path

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.stock_chip_cleaner import StockChipCleaner

"""
TWSE 三大法人籌碼清洗測試

**本檔盯的是「改制前後的版面差異」**：TWSE 的自營商欄在 2014-12-01 之前只有一個
合計欄，之後才拆成「自行買賣」與「避險」。清洗端若不分日期一律把兩個拆分欄相加，
改制前的資料就會被 `0 + 0` 蓋掉——買進、賣出兩欄變成 0，買賣超欄卻還是對的，
於是庫裡出現「買賣皆 0、買賣超非 0」這種不可能的組合，而且不會有任何錯誤。
實測：2330 在 2013-06-03 為買進 0、賣出 0、買賣超 +307,000。

不連網路、不碰正式 DB。
"""

BEFORE_REFORM: datetime.date = datetime.date(2013, 6, 3)
AFTER_REFORM: datetime.date = datetime.date(2015, 6, 3)


@pytest.fixture
def cleaner(tmp_path: Path) -> StockChipCleaner:
    """清洗結果落地改寫到暫存目錄"""

    chip_cleaner: StockChipCleaner = StockChipCleaner()
    chip_cleaner.chip_dir = tmp_path
    return chip_cleaner


def old_layout_raw() -> pd.DataFrame:
    """第一次改制前的 TWSE 版面：自營商只有合計欄"""

    return pd.DataFrame(
        [
            {
                "證券代號": "2330",
                "證券名稱": "台積電",
                "外資買進股數": 30_000_000,
                "外資賣出股數": 20_000_000,
                "外資買賣超股數": 10_000_000,
                "投信買進股數": 1_000_000,
                "投信賣出股數": 500_000,
                "投信買賣超股數": 500_000,
                "自營商買進股數": 800_000,
                "自營商賣出股數": 493_000,
                "自營商買賣超股數": 307_000,
                "三大法人買賣超股數": 10_807_000,
            }
        ]
    )


def new_layout_raw() -> pd.DataFrame:
    """第一次改制後的 TWSE 版面：自營商拆成自行買賣與避險"""

    return pd.DataFrame(
        [
            {
                "證券代號": "2330",
                "證券名稱": "台積電",
                "外資買進股數": 30_000_000,
                "外資賣出股數": 20_000_000,
                "外資買賣超股數": 10_000_000,
                "投信買進股數": 1_000_000,
                "投信賣出股數": 500_000,
                "投信買賣超股數": 500_000,
                "自營商買進股數(自行買賣)": 500_000,
                "自營商賣出股數(自行買賣)": 300_000,
                "自營商買賣超股數(自行買賣)": 200_000,
                "自營商買進股數(避險)": 300_000,
                "自營商賣出股數(避險)": 193_000,
                "自營商買賣超股數(避險)": 107_000,
                "自營商買賣超股數": 307_000,
                "三大法人買賣超股數": 10_807_000,
            }
        ]
    )


def test_old_layout_keeps_the_source_dealer_totals(cleaner: StockChipCleaner) -> None:
    """
    改制前的合計欄不可被 `0 + 0` 蓋掉

    拆分欄根本不存在，硬相加等於把來源給的數字丟掉；而買賣超欄不受影響，
    錯誤因此不會顯現成缺資料，只會變成「買賣皆 0、買賣超非 0」。
    """

    cleaned: pd.DataFrame = cleaner.clean_twse_chip(old_layout_raw(), BEFORE_REFORM)
    row: pd.Series = cleaned.iloc[0]

    assert row["自營商買進股數"] == 800_000
    assert row["自營商賣出股數"] == 493_000
    assert row["自營商買賣超股數"] == 307_000


def test_new_layout_sums_the_split_columns(cleaner: StockChipCleaner) -> None:
    """改制後兩個拆分欄要相加成合計欄（防止改過頭）"""

    cleaned: pd.DataFrame = cleaner.clean_twse_chip(new_layout_raw(), AFTER_REFORM)
    row: pd.Series = cleaned.iloc[0]

    assert row["自營商買進股數"] == 800_000
    assert row["自營商賣出股數"] == 493_000
    assert row["自營商買進股數(自行買賣)"] == 500_000
    assert row["自營商買進股數(避險)"] == 300_000


def test_dealer_totals_are_consistent_after_cleaning(
    cleaner: StockChipCleaner,
) -> None:
    """買進 − 賣出 ＝ 買賣超：兩種版面都要成立"""

    for raw, date in (
        (old_layout_raw(), BEFORE_REFORM),
        (new_layout_raw(), AFTER_REFORM),
    ):
        row: pd.Series = cleaner.clean_twse_chip(raw, date).iloc[0]

        assert row["自營商買進股數"] - row["自營商賣出股數"] == row["自營商買賣超股數"]


# === TPEX 中段版面（2014-12-01 ~ 2018-01-14）===
TPEX_MIDDLE: datetime.date = datetime.date(2016, 6, 1)


def tpex_middle_raw() -> pd.DataFrame:
    """
    TPEX 中段版面：16 欄依位置命名，只有自營商買賣超合計、沒有買進與賣出合計

    最後一列是來源附的合計列、清洗時會被移除。
    """

    row: list = [
        "6488",
        "環球晶",
        3_000,
        1_000,
        2_000,
        500,
        0,
        500,
        1_307,
        1_500,
        300,
        1_200,
        200,
        93,
        107,
        3_807,
    ]
    footer: list = ["合計"] + [0] * 15
    return pd.DataFrame([row, footer], columns=[f"c{i}" for i in range(16)])


def test_tpex_middle_layout_fills_the_dealer_totals(
    cleaner: StockChipCleaner,
) -> None:
    """
    TPEX 中段的自營商買進、賣出合計要由拆分欄相加而來

    以前這段沒有相加，reindex 把合計欄補成 0：庫裡 2014-12 ~ 2018-01 的上櫃資料
    出現十幾萬列「買賣皆 0、買賣超非 0」。
    """

    row: pd.Series = cleaner.clean_tpex_chip(tpex_middle_raw(), TPEX_MIDDLE).iloc[0]

    assert row["自營商買進股數"] == 1_700
    assert row["自營商賣出股數"] == 393
    assert row["自營商買進股數"] - row["自營商賣出股數"] == row["自營商買賣超股數"]
