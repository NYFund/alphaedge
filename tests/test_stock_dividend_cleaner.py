import datetime
from io import StringIO
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.stock_dividend_cleaner import StockDividendCleaner
from core.pipeline.utils.exceptions import ColumnLayoutError

"""
除權除息計算結果表清洗測試

以兩個官方來源的真實數值驗證欄位映射與衍生欄位，
不連網路、不連 DB；CSV 輸出導向 tmp_path，不污染 downloads 目錄。

測試資料取自 2024 年實際除權息：
- TWSE 2330 台積電 2024-06-13：純除息，前收 909.0 → 參考價 905.5
- TWSE 2543 皇昌   2024-05-30：除權息並存，證交所無法拆分現金／股票股利
- TPEX 6712 康霈* 2024-05-29：除權息並存，櫃買中心直接提供官方拆分值
- TPEX 6629 泰金-KY 2024-01-03：純除息
"""

# TWSE TWT49U 原始表格，15 欄
# 資料日期,股票代號,股票名稱,除權息前收盤價,除權息參考價,權值+息值,權/息,
# 漲停價格,跌停價格,開盤競價基準,減除股利參考價,詳細資料,申報季別,每股淨值,每股盈餘
TWSE_RAW_CSV: str = """\
113年06月13日,2330,台積電,909.0,905.50,3.499789,息,996.0,815.0,906.0,905.50,除權息資料,115年第2季,248.05,49.33
113年05月30日,2543,皇昌,59.5,48.42,11.076155,權息,57.0,43.6,51.8,51.84,除權息資料,115年第2季,18.20,1.34
"""


def build_tpex_raw() -> pd.DataFrame:
    """
    櫃買中心 `bulletin/exDailyQ` 回傳格式

    官方中文欄名、民國斜線日期、數值為字串、名稱帶全形補齊空白；
    比證交所多了 `權值`／`息值`／`現金股利`／`每仟股無償配股` 四個拆分欄位
    """

    fields: List[str] = [
        "除權息日期",
        "代號",
        "名稱",
        "除權息前收盤價",
        "除權息參考價",
        "權值",
        "息值",
        "權值+息值",
        "權/息",
        "漲停價",
        "跌停價",
        "開始交易基準價",
        "減除股利參考價",
        "現金股利",
        "每仟股無償配股",
        "現金增資股數",
        "現金增資認購價",
        "公開承銷股數",
        "員工認購股數",
        "原股東認購股數",
        "按持股比例仟股認購",
    ]
    rows: List[List[str]] = [
        # 除權息並存：官方明確給出配息 6.0 元 ＋ 每仟股配股 100 股
        [
            "113/05/29",
            "6712",
            "康霈*          ",
            "292.00",
            "265.45",
            "17.818182",
            "6.000000",
            "23.818182",
            "除權息",
            "292.00",
            "239.00",
            "265.50",
            "286.00",
            "6.00000000",
            "100.00000000",
            "0",
            "0.00",
            "0",
            "0",
            "0",
            "0.00000000",
        ],
        # 純除息
        [
            "113/01/03",
            "6629",
            "泰金-KY        ",
            "55.00",
            "53.50",
            "0.000000",
            "1.500000",
            "1.500000",
            "除息",
            "58.80",
            "48.15",
            "53.50",
            "53.50",
            "1.50000000",
            "0.00000000",
            "0",
            "0.00",
            "0",
            "0",
            "0",
            "0.00000000",
        ],
    ]
    return pd.DataFrame(rows, columns=fields)


@pytest.fixture
def cleaner(tmp_path: Path) -> StockDividendCleaner:
    """清洗器 fixture，輸出目錄改為暫存目錄"""

    dividend_cleaner: StockDividendCleaner = StockDividendCleaner()
    dividend_cleaner.dividend_dir = tmp_path
    return dividend_cleaner


def read_twse_raw() -> pd.DataFrame:
    """將合成的原始表格字串還原為爬蟲回傳的 DataFrame（代號保留為字串）"""

    return pd.read_csv(StringIO(TWSE_RAW_CSV), header=None, dtype={1: str})


def test_clean_twse_dividend_maps_columns(cleaner: StockDividendCleaner) -> None:
    """TWSE 版面依位置對應欄位，民國日期換算為西元"""

    df: pd.DataFrame = cleaner.clean_twse_dividend(read_twse_raw(), file_name="twse")

    assert list(df.columns) == cleaner.dividend_cleaned_cols
    assert list(df["stock_id"]) == ["2330", "2543"]

    tsmc: pd.Series = df.set_index("stock_id").loc["2330"]
    assert tsmc["date"] == datetime.date(2024, 6, 13)
    assert tsmc["除權息前收盤價"] == 909.0
    assert tsmc["除權息參考價"] == 905.5
    assert tsmc["權息別"] == "息"
    assert tsmc["資料來源"] == "twse"


def test_adjust_factor_reflects_price_gap(cleaner: StockDividendCleaner) -> None:
    """還原係數 = 除權息參考價 / 除權息前收盤價，恆小於 1"""

    df: pd.DataFrame = cleaner.clean_twse_dividend(
        read_twse_raw(), file_name="twse"
    ).set_index("stock_id")

    assert df.loc["2330", "還原係數"] == pytest.approx(905.50 / 909.0)
    assert df.loc["2543", "還原係數"] == pytest.approx(48.42 / 59.5)
    assert (df["還原係數"] < 1).all()


def test_cash_dividend_split_for_pure_cash(cleaner: StockDividendCleaner) -> None:
    """純除息：現金股利 = 前收盤價 − 除權息參考價，配股率為 0"""

    df: pd.DataFrame = cleaner.clean_twse_dividend(
        read_twse_raw(), file_name="twse"
    ).set_index("stock_id")

    assert df.loc["2330", "現金股利"] == pytest.approx(3.5)
    assert df.loc["2330", "配股率"] == pytest.approx(0.0)


def test_tpex_uses_official_split(cleaner: StockDividendCleaner) -> None:
    """
    上櫃：櫃買中心直接提供官方拆分值，一律採用，不做推導

    這正是把上櫃來源從第三方換成櫃買中心的最大收穫——證交所在權息並存時
    無法拆分（見 test_twse_ambiguous_split_stays_null），櫃買中心則直接給
    """

    df: pd.DataFrame = cleaner.clean_tpex_dividend(
        build_tpex_raw(), file_name="tpex"
    ).set_index("stock_id")

    # 除權息並存，官方給配息 6.0 元、每仟股配股 100 股
    assert df.loc["6712", "權息別"] == "權息"
    assert df.loc["6712", "現金股利"] == pytest.approx(6.0)
    assert df.loc["6712", "配股率"] == pytest.approx(0.1)

    # 純除息：配股率為 0
    assert df.loc["6629", "現金股利"] == pytest.approx(1.5)
    assert df.loc["6629", "配股率"] == pytest.approx(0.0)


def test_twse_ambiguous_split_stays_null(cleaner: StockDividendCleaner) -> None:
    """
    上市：權息並存時證交所欄位不足以拆分，現金股利／配股率須留 NULL

    不可硬套公式：2024 年 62 筆權息列中有 57 筆的「減除股利參考價」等於除權息參考價，
    照公式會把股票股利整包誤算成現金股利，讓放空的股利補償算錯且無錯誤訊息
    """

    df: pd.DataFrame = cleaner.clean_twse_dividend(
        read_twse_raw(), file_name="twse"
    ).set_index("stock_id")

    assert df.loc["2543", "權息別"] == "權息"
    assert pd.isna(df.loc["2543", "現金股利"])
    assert pd.isna(df.loc["2543", "配股率"])
    # 還原係數不受拆分影響，仍須算得出來
    assert df.loc["2543", "還原係數"] == pytest.approx(48.42 / 59.5)


def test_clean_tpex_dividend_normalizes_fields(cleaner: StockDividendCleaner) -> None:
    """櫃買中心的中文欄名、民國斜線日期與「除息」寫法皆須被正規化"""

    df: pd.DataFrame = cleaner.clean_tpex_dividend(build_tpex_raw(), file_name="tpex")

    assert list(df.columns) == cleaner.dividend_cleaned_cols

    row: pd.Series = df.set_index("stock_id").loc["6629"]
    assert row["date"] == datetime.date(2024, 1, 3)
    # 全形補齊空白須被去除
    assert row["證券名稱"] == "泰金-KY"
    # 「除息」須正規化為「息」，與證交所一致
    assert row["權息別"] == "息"
    assert row["開盤競價基準"] == pytest.approx(53.50)
    assert row["資料來源"] == "tpex"


def test_both_sources_share_the_same_schema(cleaner: StockDividendCleaner) -> None:
    """兩個官方來源清洗後必須落在同一組欄位，還原係數語意一致（恆小於 1）"""

    twse_df: pd.DataFrame = cleaner.clean_twse_dividend(
        read_twse_raw(), file_name="twse"
    )
    tpex_df: pd.DataFrame = cleaner.clean_tpex_dividend(
        build_tpex_raw(), file_name="tpex"
    )

    assert list(twse_df.columns) == list(tpex_df.columns)
    assert set(twse_df["權息別"]) <= {"權", "息", "權息"}
    assert set(tpex_df["權息別"]) <= {"權", "息", "權息"}
    assert (twse_df["還原係數"] < 1).all()
    assert (tpex_df["還原係數"] < 1).all()


def test_invalid_price_rows_are_dropped(cleaner: StockDividendCleaner) -> None:
    """前收盤價缺漏時整列剔除，不得產生 inf／0 還原係數而靜默失效"""

    raw: pd.DataFrame = read_twse_raw()
    raw.loc[0, 3] = 0

    df: pd.DataFrame = cleaner.clean_twse_dividend(raw, file_name="twse")

    assert list(df["stock_id"]) == ["2543"]
    assert df["還原係數"].notna().all()


def test_unexpected_column_count_raises(cleaner: StockDividendCleaner) -> None:
    """
    來源版面改制（欄位數不符）時拋例外，不讓錯位資料入庫

    **不回 None**：除權息表每輪重掃整個區間，回 None 與「這段期間沒有除權息」
    長得一樣，更新會無聲地停止——而除權息正是還原價的輸入。
    """

    raw: pd.DataFrame = read_twse_raw().drop(columns=[14])

    with pytest.raises(ColumnLayoutError):
        cleaner.clean_twse_dividend(raw, file_name="twse")


def test_csv_written_to_downloads_dir(
    cleaner: StockDividendCleaner, tmp_path: Path
) -> None:
    """清洗結果落地為 CSV，檔名由呼叫端指定"""

    cleaner.clean_twse_dividend(read_twse_raw(), file_name="twse_20240101_20241231")

    assert (tmp_path / "twse_20240101_20241231.csv").exists()
