import datetime
from io import StringIO
from pathlib import Path

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.stock_margin_cleaner import StockMarginCleaner
from core.pipeline.utils.exceptions import ColumnLayoutError

"""
信用交易（融資融券餘額）清洗測試

以合成的原始表格驗證 TWSE／TPEX 兩種版面能收斂為同一組欄位，
不連網路、不連 DB；CSV 輸出導向 tmp_path，不污染 downloads 目錄。
"""

DATE: datetime.date = datetime.date(2026, 7, 31)

# TWSE MI_MARGN（selectType=STOCK）原始表格，16 欄
# 代號,名稱,資買,資賣,現金償還,資前日,資今日,資限額,券買,券賣,現券償還,券前日,券今日,券限額,資券互抵,註記
# 第一列為「合計」（代號為空），須在清洗時被濾除
TWSE_RAW_CSV: str = """\
,合計,289781,174842,8736,6137824,6244027,191827456,27291,19709,1833,122465,113050,191827456,8604,
1101,台泥,1427,1397,50,31139,31119,1880795,3,123,0,71,191,1880795,0,
2330,台積電,1340,2674,41,30664,29289,6483092,7,25,24,130,124,6483092,15,X
"""

# TPEX 上櫃股票融資融券餘額原始表格，20 欄
# 代號,名稱,前資餘額,資買,資賣,現償,資餘額,資屬證金,資使用率,資限額,
# 前券餘額,券賣,券買,券償,券餘額,券屬證金,券使用率,券限額,資券相抵,備註
# 末兩列為統計列，須在清洗時被濾除
TPEX_RAW_CSV: str = """\
6547,高端疫苗,100,40,30,10,100,5,0.5,50000,20,15,5,0,30,0,0.1,50000,3,
8069,元太,500,0,0,0,500,9,0.24,60000,0,0,0,0,0,0,0.0,60000,0,11 C
合計(張),合計(張),2149360,69455,45743,2667,2170405,,,,31943,4638,5607,328,30646,,,,,
共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆,共914筆
"""


@pytest.fixture
def cleaner(tmp_path: Path) -> StockMarginCleaner:
    """清洗器 fixture，輸出目錄改為暫存目錄"""

    margin_cleaner: StockMarginCleaner = StockMarginCleaner()
    margin_cleaner.margin_dir = tmp_path
    return margin_cleaner


def read_raw(raw_csv: str) -> pd.DataFrame:
    """將合成的原始表格字串還原為爬蟲回傳的 DataFrame（代號保留為字串）"""

    return pd.read_csv(StringIO(raw_csv), header=None, dtype={0: str})


def test_clean_twse_margin_maps_columns(cleaner: StockMarginCleaner) -> None:
    """TWSE 版面依位置對應欄位，並濾除合計列"""

    df: pd.DataFrame = cleaner.clean_twse_margin(read_raw(TWSE_RAW_CSV), DATE)

    assert list(df.columns) == cleaner.margin_cleaned_cols
    assert list(df["stock_id"]) == ["1101", "2330"]

    tsmc: pd.Series = df.set_index("stock_id").loc["2330"]
    assert tsmc["融資買進"] == 1340
    assert tsmc["融資今日餘額"] == 29289
    assert tsmc["融券買進"] == 7
    assert tsmc["融券賣出"] == 25
    assert tsmc["融券今日餘額"] == 124
    assert tsmc["註記"] == "X"


def test_clean_tpex_margin_maps_columns(cleaner: StockMarginCleaner) -> None:
    """TPEX 版面的券賣／券買順序與 TWSE 相反，且統計列須被濾除"""

    df: pd.DataFrame = cleaner.clean_tpex_margin(read_raw(TPEX_RAW_CSV), DATE)

    assert list(df.columns) == cleaner.margin_cleaned_cols
    assert list(df["stock_id"]) == ["6547", "8069"]

    row: pd.Series = df.set_index("stock_id").loc["6547"]
    assert row["融券賣出"] == 15
    assert row["融券買進"] == 5
    assert row["融券今日餘額"] == 30
    assert row["資券互抵"] == 3
    # 「資屬證金」「使用率」不入庫
    assert "融資屬證金" not in df.columns


def test_balance_identity_holds(cleaner: StockMarginCleaner) -> None:
    """餘額恆等式：今日餘額 = 前日餘額 + 增加 − 減少（兩來源皆須成立）"""

    for df in (
        cleaner.clean_twse_margin(read_raw(TWSE_RAW_CSV), DATE),
        cleaner.clean_tpex_margin(read_raw(TPEX_RAW_CSV), DATE),
    ):
        financing: pd.Series = (
            df["融資前日餘額"] + df["融資買進"] - df["融資賣出"] - df["融資現金償還"]
        )
        short: pd.Series = (
            df["融券前日餘額"] + df["融券賣出"] - df["融券買進"] - df["融券現券償還"]
        )
        assert (financing == df["融資今日餘額"]).all()
        assert (short == df["融券今日餘額"]).all()


def test_short_ratio_and_types(cleaner: StockMarginCleaner) -> None:
    """券資比為 %，融資餘額為 0 時視為 0；張數欄位一律整數"""

    df: pd.DataFrame = cleaner.clean_twse_margin(
        read_raw(TWSE_RAW_CSV), DATE
    ).set_index("stock_id")

    # 124 / 29289 × 100 = 0.42
    assert df.loc["2330", "券資比"] == pytest.approx(0.42)

    # 融資今日餘額歸零時，券資比不得為 inf／NaN
    zero_financing: pd.DataFrame = read_raw(TWSE_RAW_CSV)
    zero_financing.loc[2, 6] = 0
    cleaned: pd.DataFrame = cleaner.clean_twse_margin(zero_financing, DATE).set_index(
        "stock_id"
    )
    assert cleaned.loc["2330", "券資比"] == 0.0

    for col in cleaner.INT_COLS:
        assert pd.api.types.is_integer_dtype(df[col])


def test_unexpected_column_count_raises(cleaner: StockMarginCleaner) -> None:
    """
    來源版面改制（欄位數不符）時拋例外，不讓錯位資料入庫

    **不回 None**：回 None 在 updater 端與「這天沒有資料」長得一樣，
    當天會被記為完成、只入庫另一個市場。
    """

    raw: pd.DataFrame = read_raw(TWSE_RAW_CSV).drop(columns=[15])

    with pytest.raises(ColumnLayoutError):
        cleaner.clean_twse_margin(raw, DATE)


def test_csv_written_to_downloads_dir(
    cleaner: StockMarginCleaner, tmp_path: Path
) -> None:
    """清洗結果落地為 CSV，檔名帶來源前綴與日期"""

    cleaner.clean_twse_margin(read_raw(TWSE_RAW_CSV), DATE)
    cleaner.clean_tpex_margin(read_raw(TPEX_RAW_CSV), DATE)

    assert (tmp_path / "twse_20260731.csv").exists()
    assert (tmp_path / "tpex_20260731.csv").exists()
