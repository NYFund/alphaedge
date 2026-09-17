import datetime
import sqlite3
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

import pandas as pd
import pytest
from loguru import logger

from core.config import (
    FUTURES_MARGIN_HISTORY_TABLE_NAME,
    STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME,
)
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.pipeline.tw.cleaners.futures_margin_cleaner import FuturesMarginCleaner
from core.pipeline.tw.loaders.futures_margin_loader import FuturesMarginLoader
from core.pipeline.tw.updaters.futures_margin_updater import FuturesMarginUpdater
from core.utils import FUTURES_MULTIPLIER

"""
台期貨保證金 ETL 的純函式測試（指數類）

**本來源真正會出事的地方不在網路層，而在三處**：
1. **生效日**取自檔案第一行的「更新日期」——解析失敗若退回今天，會產生一列
   日期錯誤但看起來完全正常的資料；
2. **欄位錯位**——來源是 CSV 且每列尾端有多餘逗號，錯一格就整批數字位移；
3. **變動序列的語意**——同一組保證金重複抓不可產生新列，否則「某日生效的保證金」
   會查到錯的那一列。

三者都不會報錯，故以固定 fixture 覆蓋。不連網路、不碰正式的 tw_futures.db。
"""

# 2026-09-01 自 TAIFEX 實際取得的內容縮影（含尾端多餘逗號與選擇權的 A／B 值）
INDEX_MARGIN_CSV: str = """更新日期:2026/08/12
商品別,結算保證金,維持保證金,原始保證金,,
臺股期貨,519000,538000,701000,
小型臺指,129750,134500,175250,
微型臺指期貨,25950,26900,35050,
臺指選擇權風險保證金(A)值,138000,143000,187000,
臺指選擇權風險保證金(B)值,69000,72000,94000,
電子期貨,724000,750000,978000,
小型電子期貨,90500,93750,122250,
金融期貨,117000,122000,158000,
小型金融期貨,29250,30500,39500,
臺灣中型100期貨,21000,22000,29000,
"""


@pytest.fixture
def cleaner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesMarginCleaner:
    """CSV 落地導向暫存區，不寫進真實的 downloads 目錄"""

    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.futures_margin_cleaner.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    return FuturesMarginCleaner()


@pytest.fixture
def loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesMarginLoader:
    """入庫器 fixture，DB 與 downloads 目錄都改為暫存"""

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    return FuturesMarginLoader()


# === 生效日 ===
def test_effective_date_comes_from_the_file_not_today(
    cleaner: FuturesMarginCleaner,
) -> None:
    """生效日取自「更新日期」，不是抓取日"""

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV)

    assert set(df["effective_date"]) == {datetime.date(2026, 8, 12)}


def test_missing_effective_date_aborts_instead_of_defaulting(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    找不到更新日期時整批放棄，**不可退回今天**

    退回今天會產生一列日期錯誤但看起來正常的資料，比整批不入庫難查得多。
    """

    without_date: str = "\n".join(INDEX_MARGIN_CSV.splitlines()[1:])

    assert cleaner.clean_index_margin(without_date) is None


def test_full_width_colon_is_accepted(cleaner: FuturesMarginCleaner) -> None:
    """全形冒號也要能解析——站方用字不穩定，不值得為此整批失敗"""

    text: str = INDEX_MARGIN_CSV.replace("更新日期:", "更新日期：")

    assert cleaner.clean_index_margin(text) is not None


# === 解析與過濾 ===
def test_only_registered_products_are_kept(cleaner: FuturesMarginCleaner) -> None:
    """
    只收乘數已登錄的商品

    選擇權的風險保證金 A／B 值語意完全不同（不是每口金額），
    「臺灣中型100期貨」則是乘數未登錄，兩者都不可入庫。
    """

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV)

    assert set(df["product"]) == {"TX", "MTX", "TMF", "TE", "ZEF", "TF", "ZFF"}
    assert all(p in FUTURES_MULTIPLIER for p in df["product"])


def test_amounts_are_parsed_into_the_right_columns(
    cleaner: FuturesMarginCleaner,
) -> None:
    """三個金額欄的順序是結算／維持／原始，錯位不會報錯只會靜默算錯"""

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV).set_index("product")

    assert df.loc["TX", "結算保證金"] == 519000
    assert df.loc["TX", "維持保證金"] == 538000
    assert df.loc["TX", "原始保證金"] == 701000


def test_trailing_commas_do_not_shift_columns(cleaner: FuturesMarginCleaner) -> None:
    """來源每列尾端有多餘逗號，不可讓欄位位移"""

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV).set_index("product")

    assert df.loc["MTX", "原始保證金"] == 175250


def test_source_is_marked_as_snapshot(cleaner: FuturesMarginCleaner) -> None:
    """來自現行一覽表的列標為 snapshot，與公告回補（S4）區分"""

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV)

    assert set(df["source"]) == {"snapshot"}


def test_header_change_aborts(cleaner: FuturesMarginCleaner) -> None:
    """表頭對不上代表站方改版，整批中止而不是硬解"""

    changed: str = INDEX_MARGIN_CSV.replace("商品別,結算保證金", "品項,結算保證金")

    assert cleaner.clean_index_margin(changed) is None


# === 乘數比例檢查 ===
def test_multiplier_ratio_holds_within_each_index_family(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    同一標的指數的大小台，每點保證金相同

    加權 3,505／電子 244.50／金融 158.00——**三者本來就不同**，
    所以檢查必須分家族做，拿 TX 去比 TE 會誤判成解析錯誤。
    """

    df: pd.DataFrame = cleaner.clean_index_margin(INDEX_MARGIN_CSV).set_index("product")
    per_point = {p: df.loc[p, "原始保證金"] / FUTURES_MULTIPLIER[p] for p in df.index}

    assert per_point["TX"] == per_point["MTX"] == per_point["TMF"] == 3505.0
    assert per_point["TE"] == per_point["ZEF"] == 244.5
    assert per_point["TF"] == per_point["ZFF"] == 158.0


def test_column_shift_is_caught_by_the_ratio_check(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    同家族內的金額被動過就會被比例檢查擋下

    這是本 cleaner 唯一能自動偵測「數字對但欄位錯」的手段。
    """

    broken: str = INDEX_MARGIN_CSV.replace(
        "小型臺指,129750,134500,175250,", "小型臺指,129750,134500,175000,"
    )

    assert cleaner.clean_index_margin(broken) is None


# === 變動序列 ===
def test_first_load_inserts_every_row(loader: FuturesMarginLoader) -> None:
    """首次入庫寫入全部商品"""

    df: pd.DataFrame = _clean(loader)

    assert loader.add_to_db(df) == len(df)
    assert loader.count_rows() == len(df)


def test_reload_of_the_same_snapshot_adds_nothing(
    loader: FuturesMarginLoader,
) -> None:
    """
    同一組保證金重複抓不產生新列

    這正是「變動序列」的實現方式——不需要另外判斷有沒有變。
    """

    df: pd.DataFrame = _clean(loader)
    loader.add_to_db(df)

    assert loader.add_to_db(df) == 0
    assert loader.count_rows() == len(df)


def test_new_effective_date_appends_a_new_generation(
    loader: FuturesMarginLoader,
) -> None:
    """保證金調整後（新的生效日）會新增一整組，舊的保留"""

    df: pd.DataFrame = _clean(loader)
    loader.add_to_db(df)

    adjusted: pd.DataFrame = df.copy()
    adjusted["effective_date"] = datetime.date(2026, 9, 1)
    adjusted["原始保證金"] = adjusted["原始保證金"] + 10000

    assert loader.add_to_db(adjusted) == len(adjusted)
    assert loader.count_rows() == len(df) * 2


def test_effective_date_is_stored_as_iso_text(loader: FuturesMarginLoader) -> None:
    """日期以 ISO 字串存放，與 tw_futures.db 其他表一致"""

    loader.add_to_db(_clean(loader))
    row = loader.conn.execute(
        f"SELECT effective_date FROM {FUTURES_MARGIN_HISTORY_TABLE_NAME} LIMIT 1"
    ).fetchone()

    assert row[0] == "2026-08-12"


def test_lookup_takes_the_latest_effective_date_on_or_before(
    loader: FuturesMarginLoader,
) -> None:
    """
    「某日生效的保證金」＝ `effective_date <= 該日` 的最大者

    不是 `= 該日`——保證金不是每天都變，用等號只有剛好調整那天查得到。
    這條查詢語意是 S5 的 API 要實作的，先在此釘住。
    """

    df: pd.DataFrame = _clean(loader)
    loader.add_to_db(df)
    adjusted: pd.DataFrame = df.copy()
    adjusted["effective_date"] = datetime.date(2026, 9, 1)
    adjusted["原始保證金"] = 800000
    loader.add_to_db(adjusted)

    def initial_margin_on(date: str) -> Optional[int]:
        row = loader.conn.execute(
            f"SELECT 原始保證金 FROM {FUTURES_MARGIN_HISTORY_TABLE_NAME} "
            f"WHERE product = 'TX' AND effective_date <= ? "
            f"ORDER BY effective_date DESC LIMIT 1",
            (date,),
        ).fetchone()
        return None if row is None else row[0]

    assert initial_margin_on("2026-08-20") == 701000  # 調整前那一組仍生效
    assert initial_margin_on("2026-09-05") == 800000  # 調整後
    assert initial_margin_on("2026-08-01") is None  # 早於表內任何生效日


def test_index_exists_for_product_lookup(loader: FuturesMarginLoader) -> None:
    """建表時一併建立 (product, effective_date) 索引，主鍵前綴幫不上該查詢"""

    names = {
        row[0]
        for row in loader.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )
    }

    assert "idx_futures_margin_product" in names


def _clean(loader: FuturesMarginLoader) -> pd.DataFrame:
    """以 loader 的暫存目錄清洗一份 fixture（避免寫到真實 downloads）"""

    cleaner: FuturesMarginCleaner = FuturesMarginCleaner.__new__(FuturesMarginCleaner)
    cleaner.margin_dir = loader.margin_dir
    cleaner.setup()
    return cleaner.clean_index_margin(INDEX_MARGIN_CSV)


def test_empty_db_reports_no_rows(loader: FuturesMarginLoader) -> None:
    """建表後即使沒有資料也不應拋錯"""

    assert loader.count_rows() == 0
    assert loader.get_effective_dates() == set()


def test_empty_dataframe_is_a_noop(loader: FuturesMarginLoader) -> None:
    """空 DataFrame 不入庫也不拋錯"""

    assert loader.add_to_db(pd.DataFrame()) == 0


def test_table_columns_match_cleaner_output(loader: FuturesMarginLoader) -> None:
    """
    清洗後的欄位須與資料表宣告完全一致（含順序）

    cleaner 加了欄位卻忘了改 schema 時，錯誤要到入庫才浮出來。
    """

    conn: sqlite3.Connection = loader.conn
    table_cols = [
        row[1]
        for row in conn.execute(
            f"PRAGMA table_info('{FUTURES_MARGIN_HISTORY_TABLE_NAME}')"
        )
    ]
    cleaner: FuturesMarginCleaner = FuturesMarginCleaner.__new__(FuturesMarginCleaner)
    cleaner.margin_dir = loader.margin_dir
    cleaner.setup()

    assert cleaner.margin_cleaned_cols == table_cols


# ============================================================
# 股票類：一份 CSV 四個段落，比例段與 ETF 金額段各自入庫
# ============================================================

STOCK_MARGIN_CSV: str = """一、股票期貨契約保證金一覽表
(一) 標的證券為股票之股票期貨契約
更新日期:2026/08/28
序號,股票期貨英文代碼,股票期貨標的證券代號,股票期貨中文簡稱,股票期貨標的證券,保證金所屬級距,結算保證金適用比例,維持保證金適用比例,原始保證金適用比例,
1,DFF    ,1101,台泥期貨,"臺灣水泥股份有限公司",級距1,10.00%,10.35%,13.50%,
2,CFF    ,1301,台塑期貨,"台灣塑膠工業股份有限公司",級距2,12.00%,12.42%,16.20%,
3,CAF    ,1303,南亞期貨,"南亞塑膠工業股份有限公司",級距3,15.00%,15.53%,20.25%,
4,NAF    ,3105,穩懋期貨,"穩懋半導體股份有限公司",,16.00%,16.56%,21.60%,
5,CDF    ,2330,台積電期貨,"台灣積體電路製造股份有限公司",級距1,10.00%,10.35%,13.50%,
(二) 標的證券為受益憑證之股票期貨契約
更新日期:2026/08/12
序號,股票期貨英文代碼,股票期貨標的證券代號,股票期貨中文簡稱,股票期貨標的證券,結算保證金,維持保證金,原始保證金,
1,NYF    ,0050,元大台灣50ETF期貨,元大台灣卓越50證券投資信託基金,64000,67000,87000,
2,SRF    ,0050,小型元大台灣50ETF期貨,元大台灣卓越50證券投資信託基金,6400,6700,8700,
二、股票選擇權契約保證金一覽表
(一) 標的證券為股票之股票選擇權契約
更新日期:2026/08/14
序號,股票選擇權英文代碼,股票選擇權標的證券代號,股票選擇權中文簡稱,股票選擇權標的證券,保證金所屬級距,結算保證金適用比例,,維持保證金適用比例
,,,,,,a%,b%,a%
1,DFO    ,1101,台泥選擇權,"臺灣水泥股份有限公司",級距1,10.00%,5.000%,10.35%
(二) 標的證券為受益憑證之股票選擇權契約
更新日期:2026/08/12
序號,股票選擇權英文代碼,股票選擇權標的證券代號,股票選擇權中文簡稱,股票選擇權標的證券,保證金,結算保證金,維持保證金,原始保證金
1,NYA    ,0050,元大台灣50ETF選擇權,元大台灣卓越50證券投資信託基金,A值,64000,67000,87000
,,,,,B值,32000,34000,44000
"""


@pytest.fixture
def stock_cleaner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> FuturesMarginCleaner:
    """CSV 落地導向暫存區"""

    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.futures_margin_cleaner"
        ".FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    return FuturesMarginCleaner()


@pytest.fixture
def rate_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesMarginLoader:
    """入庫器 fixture，DB 與 downloads 目錄都改為暫存"""

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    return FuturesMarginLoader()


def clean_stock(
    stock_cleaner: FuturesMarginCleaner,
) -> Dict[str, Optional[pd.DataFrame]]:
    """跑一次完整清洗"""

    return stock_cleaner.clean_stock_margin(STOCK_MARGIN_CSV)


# === 段落切分 ===
def test_options_sections_are_dropped(stock_cleaner: FuturesMarginCleaner) -> None:
    """
    選擇權兩段必須整段丟掉

    `DFO`／`NYA` 與股期的 `DFF`／`NYF` 只差一個字母，混進來完全不會報錯，
    但它們的保證金語意是風險保證金參數，不是每口保證金。
    """

    out = clean_stock(stock_cleaner)
    codes = set(out["rate"]["product_id"]) | set(out["amount"]["product"])

    assert "DFO" not in codes
    assert "NYA" not in codes
    assert codes == {"DFF", "CFF", "CAF", "NAF", "CDF", "NYF", "SRF"}


def test_rate_and_amount_are_split_by_column_type(
    stock_cleaner: FuturesMarginCleaner,
) -> None:
    """
    **分表依據是「金額 vs 比例」不是「指數 vs 股票」**

    ETF 股期給的是每口固定金額，語意與臺股期貨相同，故走金額路徑。
    """

    out = clean_stock(stock_cleaner)

    assert set(out["rate"]["product_id"]) == {"DFF", "CFF", "CAF", "NAF", "CDF"}
    assert set(out["amount"]["product"]) == {"NYF", "SRF"}


# === 每段各有生效日 ===
def test_each_section_keeps_its_own_effective_date(
    stock_cleaner: FuturesMarginCleaner,
) -> None:
    """
    比例段是 08/28、金額段是 08/12——**不是同一天**

    用全檔第一個找到的日期套用到全部，會讓 ETF 股期的生效日錯 16 天。
    """

    out = clean_stock(stock_cleaner)

    assert set(out["rate"]["effective_date"]) == {datetime.date(2026, 8, 28)}
    assert set(out["amount"]["effective_date"]) == {datetime.date(2026, 8, 12)}


# === 比例轉換 ===
def test_rates_are_stored_as_decimals(stock_cleaner: FuturesMarginCleaner) -> None:
    """
    `13.50%` → `0.1350`

    存成 `13.50` 會讓保證金差 100 倍卻不會報錯。
    """

    df: pd.DataFrame = clean_stock(stock_cleaner)["rate"].set_index("product_id")

    assert df.loc["DFF", "原始保證金適用比例"] == 0.135
    assert df.loc["CFF", "原始保證金適用比例"] == 0.162
    assert df.loc["CAF", "原始保證金適用比例"] == 0.2025


def test_three_rate_columns_are_in_order(stock_cleaner: FuturesMarginCleaner) -> None:
    """結算／維持／原始三欄的順序，錯位不會報錯只會靜默算錯"""

    df: pd.DataFrame = clean_stock(stock_cleaner)["rate"].set_index("product_id")

    assert df.loc["DFF", "結算保證金適用比例"] == 0.10
    assert df.loc["DFF", "維持保證金適用比例"] == 0.1035
    assert df.loc["DFF", "原始保證金適用比例"] == 0.135


# === 處置股票：級距為空 ===
def test_empty_tier_is_kept_as_null(stock_cleaner: FuturesMarginCleaner) -> None:
    """
    級距為空的是處置／注意股票，比例更高——**不可因此把該檔丟掉**

    2026-09-01 實查 296 檔中有 15 檔如此（台玻、旺宏、南亞科、穩懋…）。
    """

    df: pd.DataFrame = clean_stock(stock_cleaner)["rate"].set_index("product_id")

    assert "NAF" in df.index
    # pandas 會把 object 欄的 None 正規化成 NaN；入庫後才是真正的 NULL
    # （見 test_null_tier_survives_the_round_trip）
    assert pd.isna(df.loc["NAF", "保證金所屬級距"])
    assert df.loc["NAF", "原始保證金適用比例"] == 0.216


# === 逗號與代碼 ===
def test_company_names_with_commas_do_not_shift_columns(
    stock_cleaner: FuturesMarginCleaner,
) -> None:
    """
    標的證券名稱以引號包住且含逗號，必須用 csv 模組解析

    用 `split(",")` 會讓後面的欄位整排位移。
    """

    df: pd.DataFrame = clean_stock(stock_cleaner)["rate"].set_index("product_id")

    assert df.loc["CDF", "underlying_stock_id"] == "2330"
    assert df.loc["CDF", "原始保證金適用比例"] == 0.135


def test_product_codes_are_stripped(stock_cleaner: FuturesMarginCleaner) -> None:
    """
    來源的代碼帶尾端空白（`DFF    `），必須 strip 才對得回標的池

    `futures_stock_universe.product_id` 是 3 碼無空白。
    """

    out = clean_stock(stock_cleaner)

    assert all(code == code.strip() for code in out["rate"]["product_id"])
    assert all(code == code.strip() for code in out["amount"]["product"])


# === 金額段 ===
def test_etf_amounts_use_the_shared_schema(
    stock_cleaner: FuturesMarginCleaner,
) -> None:
    """ETF 股期的欄位與 `futures_margin_history` 一致，才能共用同一個 loader"""

    df: pd.DataFrame = clean_stock(stock_cleaner)["amount"]

    assert list(df.columns) == [
        "effective_date",
        "product",
        "product_name",
        "結算保證金",
        "維持保證金",
        "原始保證金",
        "source",
    ]
    assert df.set_index("product").loc["NYF", "原始保證金"] == 87000


# === 站方改版 ===
def test_missing_futures_section_aborts(stock_cleaner: FuturesMarginCleaner) -> None:
    """找不到「一、股票期貨」段落代表改版，整份放棄"""

    assert stock_cleaner.clean_stock_margin("只有一行沒有段落標記") is None


def test_header_change_yields_none_for_that_section(
    stock_cleaner: FuturesMarginCleaner,
) -> None:
    """
    某一段的表頭對不上時只有該段為 None，另一段照常回傳

    兩段的來源與更新日期本來就獨立，一段壞掉不該讓另一段也不入庫。
    """

    broken: str = STOCK_MARGIN_CSV.replace(
        "保證金所屬級距,結算保證金適用比例", "級距,結算比例"
    )
    out = stock_cleaner.clean_stock_margin(broken)

    assert out["rate"] is None
    assert out["amount"] is not None and len(out["amount"]) == 2


# === 入庫 ===
def test_first_load_inserts_every_row_for_rates(
    rate_loader: FuturesMarginLoader,
) -> None:
    """首次入庫寫入全部商品"""

    df: pd.DataFrame = _clean_rates_with(rate_loader)

    assert rate_loader.add_rates_to_db(df) == len(df)
    assert rate_loader.count_rows(STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME) == len(
        df
    )


def test_reload_adds_nothing(rate_loader: FuturesMarginLoader) -> None:
    """比例沒變就不產生新列——變動序列的實現方式"""

    df: pd.DataFrame = _clean_rates_with(rate_loader)
    rate_loader.add_rates_to_db(df)

    assert rate_loader.add_rates_to_db(df) == 0


def test_new_effective_date_appends(rate_loader: FuturesMarginLoader) -> None:
    """比例調整後（新的生效日）新增一整組，舊的保留"""

    df: pd.DataFrame = _clean_rates_with(rate_loader)
    rate_loader.add_rates_to_db(df)

    adjusted: pd.DataFrame = df.copy()
    adjusted["effective_date"] = datetime.date(2026, 9, 1)
    adjusted["原始保證金適用比例"] = 0.2025

    assert rate_loader.add_rates_to_db(adjusted) == len(adjusted)
    assert (
        rate_loader.count_rows(STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME)
        == len(df) * 2
    )


def test_null_tier_survives_the_round_trip(rate_loader: FuturesMarginLoader) -> None:
    """級距為 NULL 的列要能入庫並讀回仍是 NULL"""

    rate_loader.add_rates_to_db(_clean_rates_with(rate_loader))
    row = rate_loader.conn.execute(
        f"SELECT 保證金所屬級距, 原始保證金適用比例 "
        f"FROM {STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME} WHERE product_id = 'NAF'"
    ).fetchone()

    assert row == (None, 0.216)


def test_lookup_takes_the_latest_effective_date_on_or_before_for_rates(
    rate_loader: FuturesMarginLoader,
) -> None:
    """查詢語意與金額表相同：`effective_date <= 該日` 的最大者（供 S5 用）"""

    df: pd.DataFrame = _clean_rates_with(rate_loader)
    rate_loader.add_rates_to_db(df)
    adjusted: pd.DataFrame = df.copy()
    adjusted["effective_date"] = datetime.date(2026, 9, 1)
    adjusted["原始保證金適用比例"] = 0.2025
    rate_loader.add_rates_to_db(adjusted)

    def rate_on(date: str) -> Optional[float]:
        row = rate_loader.conn.execute(
            f"SELECT 原始保證金適用比例 "
            f"FROM {STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME} "
            f"WHERE product_id = 'DFF' AND effective_date <= ? "
            f"ORDER BY effective_date DESC LIMIT 1",
            (date,),
        ).fetchone()
        return None if row is None else row[0]

    assert rate_on("2026-08-30") == 0.135
    assert rate_on("2026-09-05") == 0.2025
    assert rate_on("2026-08-01") is None


def test_table_columns_match_cleaner_output_for_rates(
    rate_loader: FuturesMarginLoader, stock_cleaner: FuturesMarginCleaner
) -> None:
    """清洗後的欄位須與資料表宣告完全一致（含順序）"""

    table_cols = [
        row[1]
        for row in rate_loader.conn.execute(
            f"PRAGMA table_info('{STOCK_FUTURES_MARGIN_RATE_HISTORY_TABLE_NAME}')"
        )
    ]

    assert stock_cleaner.rate_cleaned_cols == table_cols


def _clean_rates_with(rate_loader: FuturesMarginLoader) -> pd.DataFrame:
    """以 loader 的暫存目錄清洗一份 fixture（避免寫到真實 downloads）"""

    cleaner: FuturesMarginCleaner = FuturesMarginCleaner.__new__(FuturesMarginCleaner)
    cleaner.margin_dir = rate_loader.margin_dir
    cleaner.setup()
    return cleaner.clean_stock_margin(STOCK_MARGIN_CSV)["rate"]


# ============================================================
# 調整公告：歷史回補的來源（S4）
# ============================================================

# 2026-09-01 自 TAIFEX 實際取得的附件縮影。
# **金額欄的順序與一覽表相反**（公告是原始→維持→結算），且**選擇權列的
# 「契約ABC值」非空**。
ANNOUNCEMENT_CSV: str = """契約中文簡稱,契約代碼,契約ABC值,調整後原始保證金,調整後維持保證金,調整後結算保證金,調整前原始保證金,調整前維持保證金,調整前結算保證金
臺股期貨,TX, ,526000,403000,389000,477000,366000,353000
小型臺指期貨,MTX, ,131500,100750,97250,119250,91500,88250
臺指選擇權風險保證金,TXO,A,187000,143000,138000,170000,130000,125000
"""

# 股票期貨的公告用**同一個表頭**，但值是「適用比例」不是金額
ANNOUNCEMENT_RATE_CSV: str = """契約中文簡稱,契約代碼,契約ABC值,調整後原始保證金,調整後維持保證金,調整後結算保證金,調整前原始保證金,調整前維持保證金,調整前結算保證金
晶技期貨,ITF, ,0.27,0.207,0.2,0.135,0.1035,0.1
信昌電期貨,PKF, ,0.243,0.1863,0.18,0.162,0.1242,0.12
"""

ANNOUNCEMENT_TITLE: str = (
    "公告調整臺股期貨(TX)等13檔股價指數類契約之保證金，"
    "並自115年4月22日一般交易時段結束後起實施，請查照。"
)
ANNOUNCEMENT_DATE: datetime.date = datetime.date(2026, 4, 21)


# === 生效日（民國年）===
def test_effective_date_comes_from_the_title(cleaner: FuturesMarginCleaner) -> None:
    """
    生效日取自標題的民國年，**不是公告日**

    TAIFEX 的規則是「自公告日之次一一般交易時段結束後起實施」。
    """

    out = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )

    assert set(out["margin"]["effective_date"]) == {datetime.date(2026, 4, 22)}


def test_reference_letter_date_is_not_mistaken_for_effective_date(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    標題前段引用主管機關來函的日期不帶「自」字，不可被當成生效日

    Ex:「依金管會115年6月10日金管證期字第…函…並自115年7月6日…起實施」
    """

    title: str = (
        "依金融監督管理委員會115年6月10日金管證期字第1150343223號函辦理，"
        "公告調整臺股期貨(TX)之保證金，並自115年7月6日一般交易時段起實施。"
    )
    out = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, title, datetime.date(2026, 7, 1)
    )

    assert set(out["margin"]["effective_date"]) == {datetime.date(2026, 7, 6)}


def test_unparsable_title_falls_back_to_announcement_date_plus_one(
    cleaner: FuturesMarginCleaner,
) -> None:
    """標題解不出生效日時退回公告日 +1，不猜也不中止"""

    out = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, "公告調整保證金", ANNOUNCEMENT_DATE
    )

    assert set(out["margin"]["effective_date"]) == {datetime.date(2026, 4, 22)}


def test_out_of_range_effective_date_falls_back(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    解出的生效日離公告日太遠就視為解錯

    標題裡混進其他年份的日期時，用錯的日期會讓整條變動序列錯位。
    """

    title: str = "公告調整臺股期貨(TX)之保證金，並自100年1月1日起實施。"
    out = cleaner.clean_margin_announcement(ANNOUNCEMENT_CSV, title, ANNOUNCEMENT_DATE)

    assert set(out["margin"]["effective_date"]) == {datetime.date(2026, 4, 22)}


# === 欄位順序與過濾 ===
def test_announcement_column_order_is_reversed_from_the_listing(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    **公告是「原始→維持→結算」，一覽表是「結算→維持→原始」**

    照抄一覽表的取值位置會讓三個數字互換而不會報錯。
    """

    df = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["margin"].set_index("product")

    assert df.loc["TX", "原始保證金"] == 526000
    assert df.loc["TX", "維持保證金"] == 403000
    assert df.loc["TX", "結算保證金"] == 389000


def test_option_rows_are_filtered_by_abc_column(
    cleaner: FuturesMarginCleaner,
) -> None:
    """`契約ABC值` 非空者是選擇權的風險保證金參數，不是每口保證金"""

    df = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["margin"]

    assert set(df["product"]) == {"TX", "MTX"}
    assert "TXO" not in set(df["product"])


def test_previous_values_are_kept_for_chain_validation(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    「調整前」三欄必須保留——它是鏈式驗證的唯一依據

    入庫時才丟掉（只取 `margin_cleaned_cols`）。
    """

    df = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["margin"].set_index("product")

    assert df.loc["TX", "調整前原始保證金"] == 477000
    assert df.loc["TX", "調整前結算保證金"] == 353000


def test_source_is_marked_as_announcement(cleaner: FuturesMarginCleaner) -> None:
    """公告來源標為 announcement，與現行一覽表的 snapshot 區分"""

    df = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["margin"]

    assert set(df["source"]) == {"announcement"}


# === 同一表頭兩種值 ===
def test_stock_futures_announcements_are_routed_to_the_rate_table(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    **股票期貨的公告用同一個表頭，但值是「適用比例」不是金額**

    把 `0.27` 當成 27 元寫進金額表不會報錯，只會讓保證金差六個數量級。
    唯一可靠的區分是看值有沒有小數點。
    """

    out = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_RATE_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )

    assert out["margin"] is None
    assert out["rate"] is not None
    rates = out["rate"].set_index("product_id")
    assert rates.loc["ITF", "原始保證金適用比例"] == 0.27
    assert rates.loc["ITF", "調整前原始保證金適用比例"] == 0.135


def test_announcement_rates_are_already_decimals(
    cleaner: FuturesMarginCleaner,
) -> None:
    """
    公告的比例本來就是小數（`0.27`），不像一覽表帶百分號

    再除以 100 會讓比例差 100 倍。
    """

    rates = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_RATE_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["rate"].set_index("product_id")

    assert rates.loc["PKF", "原始保證金適用比例"] == 0.243


def test_announcement_rate_rows_have_no_tier(cleaner: FuturesMarginCleaner) -> None:
    """公告不提供級距，只給比例——級距為 NULL 不代表解析失敗"""

    rates = cleaner.clean_margin_announcement(
        ANNOUNCEMENT_RATE_CSV, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["rate"]

    assert rates["保證金所屬級距"].isna().all()


# === 新增商品型與雜訊 ===
def test_six_column_variant_is_accepted(cleaner: FuturesMarginCleaner) -> None:
    """
    「新增商品」型的公告只有前六欄（沒有調整前）

    2026-09-01 盤點 81 份附件中有 1 份如此，不可因此整份放棄。
    """

    six_col: str = (
        "契約中文簡稱,契約代碼,契約ABC值,調整後原始保證金,調整後維持保證金,調整後結算保證金\n"
        "元大台灣50ETF期貨,NYF, ,40000,31000,29000\n"
    )
    df = cleaner.clean_margin_announcement(
        six_col, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
    )["margin"]

    assert len(df) == 1
    assert df.set_index("product").loc["NYF", "原始保證金"] == 40000
    assert pd.isna(df.set_index("product").loc["NYF", "調整前原始保證金"])


def test_non_margin_announcement_yields_none(cleaner: FuturesMarginCleaner) -> None:
    """
    只有選擇權列的附件回傳 None——那是「沒有期貨列」不是解析失敗

    寬關鍵字會抓到部位限制、SPAN 參數等公告，靠這一關擋掉。
    """

    options_only: str = (
        "契約中文簡稱,契約代碼,契約ABC值,調整後原始保證金,調整後維持保證金,調整後結算保證金,"
        "調整前原始保證金,調整前維持保證金,調整前結算保證金\n"
        "臺指選擇權風險保證金,TXO,A,187000,143000,138000,170000,130000,125000\n"
    )

    assert (
        cleaner.clean_margin_announcement(
            options_only, ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
        )
        is None
    )


def test_wrong_header_yields_none(cleaner: FuturesMarginCleaner) -> None:
    """表頭不符代表不是保證金調整附件，跳過而不硬解"""

    assert (
        cleaner.clean_margin_announcement(
            "欄一,欄二\n1,2\n", ANNOUNCEMENT_TITLE, ANNOUNCEMENT_DATE
        )
        is None
    )


# === 入庫：公告覆蓋一覽表 ===
def test_announcement_overrides_snapshot_on_the_same_key(
    loader: FuturesMarginLoader,
) -> None:
    """
    **公告比一覽表權威**：同一個 (生效日, 商品) 以公告為準

    一覽表只說「現在是多少」，公告明載生效日與調整前／後。先寫入的 snapshot
    若擋住公告，該次調整就查無 `source='announcement'` 的列，
    鏈式驗證會因此出現假斷點。
    """

    snapshot: pd.DataFrame = _clean(loader)
    loader.add_to_db(snapshot)
    before: int = loader.count_rows()

    announcement: pd.DataFrame = snapshot.copy()
    announcement["source"] = "announcement"
    announcement["原始保證金"] = 999999
    loader.add_announcements_to_db(announcement)

    assert loader.count_rows() == before  # 覆蓋不是新增
    row = loader.conn.execute(
        f"SELECT 原始保證金, source FROM {FUTURES_MARGIN_HISTORY_TABLE_NAME} "
        f"WHERE product = 'TX'"
    ).fetchone()
    assert row == (999999, "announcement")


def test_snapshot_does_not_override_announcement(
    loader: FuturesMarginLoader,
) -> None:
    """反向不成立：一覽表不可覆蓋公告"""

    announcement: pd.DataFrame = _clean(loader)
    announcement["source"] = "announcement"
    loader.add_announcements_to_db(announcement)

    snapshot: pd.DataFrame = announcement.copy()
    snapshot["source"] = "snapshot"
    snapshot["原始保證金"] = 111111
    loader.add_to_db(snapshot)

    row = loader.conn.execute(
        f"SELECT source FROM {FUTURES_MARGIN_HISTORY_TABLE_NAME} WHERE product = 'TX'"
    ).fetchone()
    assert row[0] == "announcement"


def test_effective_dates_can_be_filtered_by_source(
    loader: FuturesMarginLoader,
) -> None:
    """
    續跑判斷必須能限定 source

    不限定的話，snapshot 恰好同一天會讓該則公告被整則跳過，
    其餘商品因此全部沒進表（2026-09-01 實測踩到）。
    """

    loader.add_to_db(_clean(loader))

    assert loader.get_effective_dates() == {"2026-08-12"}
    assert loader.get_effective_dates(source="announcement") == set()
    assert loader.get_effective_dates(source="snapshot") == {"2026-08-12"}


# ============================================================
# 回補的兩道防線：附件網址去重 ＋ 缺口回報
# ============================================================


def make_updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """不連網、不碰正式 DB 的 updater（只用到 loader 與純函式）"""

    from core.pipeline.tw.updaters.futures_margin_updater import FuturesMarginUpdater

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    updater = FuturesMarginUpdater.__new__(FuturesMarginUpdater)
    updater.loader = FuturesMarginLoader()
    updater.dao = updater.loader.dao
    updater.conn = updater.loader.conn
    updater.ANNOUNCEMENT_DELAY_SECONDS = 0
    return updater


def test_shared_attachment_url_keeps_only_the_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    **多則公告共用同一個附件網址時，只有最新那則的內容可信**

    部分附件用固定檔名（`保證金調整情形列表.csv`），站方會覆寫它——
    2026-09-01 實測，2022/04/14 與 2026/03/31 共用同一個網址，
    今天下載舊公告拿到的是 2026 的內容。照單全收會把 2026 的金額寫成 2022 的歷史。
    """

    updater = make_updater(tmp_path, monkeypatch)
    shared: str = "https://taifex/attach/保證金調整情形列表.csv"
    announcements = [
        {"date": "2022/04/14", "link": "a", "title": "t"},
        {"date": "2024/01/01", "link": "b", "title": "t"},
        {"date": "2026/03/31", "link": "c", "title": "t"},
    ]
    urls = {"a": shared, "b": "https://taifex/attach/0101.csv", "c": shared}

    class Stub:
        def resolve_announcement_csv(self, link: str):
            return urls[link]

    updater.crawler = Stub()
    resolved = updater.resolve_csv_urls(announcements)

    assert resolved[0][1] is None  # 2022 那則被排除
    assert resolved[1][1] == "https://taifex/attach/0101.csv"  # 專屬檔名不受影響
    assert resolved[2][1] == shared  # 最新那則保留


def test_unique_attachment_urls_are_all_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """檔名各自不同時全部保留，不可誤殺"""

    updater = make_updater(tmp_path, monkeypatch)
    announcements = [
        {"date": "2024/01/01", "link": "a", "title": "t"},
        {"date": "2024/02/01", "link": "b", "title": "t"},
    ]

    class Stub:
        def resolve_announcement_csv(self, link: str):
            return f"https://taifex/attach/{link}.csv"

    updater.crawler = Stub()
    resolved = updater.resolve_csv_urls(announcements)

    assert [url for _, url in resolved] == [
        "https://taifex/attach/a.csv",
        "https://taifex/attach/b.csv",
    ]


def test_chain_mismatch_is_reported_not_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    「調整前」對不上只回報缺口，**不可拒收**

    公告載明的「調整後」本來就是權威；對不上代表我們的歷史有缺口
    （前面某則的附件被站方覆寫而取不到），不代表這一則的值有問題。
    2026-09-01 實測：拒收會讓一個缺口往後連鎖成 46 則，表內只剩 186 列。
    """

    updater = make_updater(tmp_path, monkeypatch)
    updater.loader.add_to_db(
        pd.DataFrame(
            [
                {
                    "effective_date": datetime.date(2024, 1, 1),
                    "product": "TX",
                    "product_name": "臺股期貨",
                    "結算保證金": 100000,
                    "維持保證金": 110000,
                    "原始保證金": 120000,
                    "source": "announcement",
                }
            ]
        )
    )

    # 這則公告說「調整前是 200000」，但表內是 120000 → 中間有缺口
    df = pd.DataFrame(
        [
            {
                "effective_date": "2024-06-01",
                "product": "TX",
                "product_name": "臺股期貨",
                "結算保證金": 210000,
                "維持保證金": 220000,
                "原始保證金": 230000,
                "source": "announcement",
                "調整前結算保證金": 180000,
                "調整前維持保證金": 190000,
                "調整前原始保證金": 200000,
            }
        ]
    )
    ok, mismatches = updater.check_announcement_consistency(df)

    assert ok is False
    assert len(mismatches) == 1
    assert "TX" in mismatches[0]


def test_first_appearance_of_a_product_is_not_a_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """表內查無前值的商品（第一次出現）不參與比對，不算缺口"""

    updater = make_updater(tmp_path, monkeypatch)
    df = pd.DataFrame(
        [
            {
                "effective_date": "2024-06-01",
                "product": "NEW",
                "product_name": "新商品",
                "結算保證金": 1,
                "維持保證金": 2,
                "原始保證金": 3,
                "source": "announcement",
                "調整前結算保證金": 10,
                "調整前維持保證金": 20,
                "調整前原始保證金": 30,
            }
        ]
    )
    ok, mismatches = updater.check_announcement_consistency(df)

    assert ok is True
    assert mismatches == []


def test_margin_in_effect_uses_strictly_earlier_dates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    「這次調整之前是多少」用 `<` 不是 `<=`

    用 `<=` 會拿到這次調整本身的值，比對永遠成立，守門形同虛設。
    """

    updater = make_updater(tmp_path, monkeypatch)
    updater.loader.add_to_db(
        pd.DataFrame(
            [
                {
                    "effective_date": datetime.date(2024, 1, 1),
                    "product": "TX",
                    "product_name": "臺股期貨",
                    "結算保證金": 1,
                    "維持保證金": 2,
                    "原始保證金": 100,
                    "source": "announcement",
                },
                {
                    "effective_date": datetime.date(2024, 6, 1),
                    "product": "TX",
                    "product_name": "臺股期貨",
                    "結算保證金": 1,
                    "維持保證金": 2,
                    "原始保證金": 200,
                    "source": "announcement",
                },
            ]
        )
    )

    assert updater.get_margin_in_effect("TX", "2024-06-01") == 100
    assert updater.get_margin_in_effect("TX", "2024-01-01") is None


# ============================================================
# 歷史回補只抓未處理的公告
# ============================================================
# 只有選擇權列的附件：有 CSV，但沒有期貨列
ANNOUNCEMENT_OPTIONS_ONLY_CSV: str = """契約中文簡稱,契約代碼,契約ABC值,調整後原始保證金,調整後維持保證金,調整後結算保證金,調整前原始保證金,調整前維持保證金,調整前結算保證金
臺指選擇權風險保證金,TXO,A,187000,143000,138000,170000,130000,125000
"""


class _RecordingCrawler:
    """記錄明細頁解析與附件下載次數的公告 crawler 替身"""

    def __init__(self) -> None:
        # 指數類（金額）與股票類（比例）**同一個生效日**，外加選擇權公告與掃描 PDF
        self.announcements: List[Dict[str, str]] = [
            {"date": "2026/04/21", "title": ANNOUNCEMENT_TITLE, "link": "index"},
            {"date": "2026/04/21", "title": ANNOUNCEMENT_TITLE, "link": "stock"},
            {"date": "2026/04/21", "title": ANNOUNCEMENT_TITLE, "link": "options"},
            {"date": "2026/04/21", "title": ANNOUNCEMENT_TITLE, "link": "scan.pdf"},
        ]
        self.contents: Dict[str, Optional[str]] = {
            "https://taifex/attach/index.csv": ANNOUNCEMENT_CSV,
            "https://taifex/attach/stock.csv": ANNOUNCEMENT_RATE_CSV,
            "https://taifex/attach/options.csv": ANNOUNCEMENT_OPTIONS_ONLY_CSV,
        }
        self.resolved_links: List[str] = []
        self.downloaded_urls: List[str] = []

    def crawl_announcements(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[Dict[str, str]]:
        """回傳固定的公告清單"""

        return self.announcements

    def resolve_announcement_csv(self, link: str) -> Optional[str]:
        """PDF 連結沒有附件，其餘組出固定網址"""

        self.resolved_links.append(link)
        if link.endswith(".pdf"):
            return None
        return f"https://taifex/attach/{link}.csv"

    def crawl_announcement_csv(self, csv_url: str) -> Optional[str]:
        """回傳附件原文；內容設為 None 時模擬下載失敗"""

        self.downloaded_urls.append(csv_url)
        return self.contents[csv_url]


def make_history_updater(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Tuple[FuturesMarginUpdater, _RecordingCrawler]:
    """接上公告 crawler 替身、清洗器與暫存處理紀錄的 updater"""

    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.futures_margin_cleaner.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    updater: FuturesMarginUpdater = make_updater(tmp_path, monkeypatch)
    crawler: _RecordingCrawler = _RecordingCrawler()
    updater.crawler = crawler
    updater.cleaner = FuturesMarginCleaner()
    updater.announcement_metadata_path = tmp_path / "meta" / "announcements.json"
    return updater, crawler


@pytest.fixture
def summaries() -> Iterator[List[str]]:
    """收集回補統計的 log 訊息"""

    messages: List[str] = []
    sink_id: int = logger.add(
        lambda message: messages.append(message.record["message"]),
        filter=lambda record: "回補統計" in record["message"],
    )
    yield messages
    logger.remove(sink_id)


def test_second_backfill_skips_processed_announcements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, summaries: List[str]
) -> None:
    """
    第二次回補不再開明細頁、不再下載附件；沒有 CSV 的公告照常重試

    舊版算了已入庫的生效日卻沒拿來跳過，每次回補都重抓全部附件（約數百次請求）。
    """

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    updater.update_history()

    assert len(crawler.downloaded_urls) == 3
    assert updater.dao.count_rows() == 2
    assert updater.dao.count_rows(FuturesMarginDAO.RATE_TABLE_NAME) == 2

    crawler.resolved_links.clear()
    crawler.downloaded_urls.clear()
    updater.update_history()

    assert crawler.downloaded_urls == []
    assert crawler.resolved_links == ["scan.pdf"]
    assert "已存在跳過 3 則" in summaries[-1]


def test_same_effective_date_does_not_mark_other_announcements_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    同一個生效日的另一則公告沒入庫時，下次回補要補它

    以生效日判斷「已入庫」會把同日的另一則誤判為已處理；這裡指數類已入庫、
    股票類第一次下載失敗，兩則的生效日相同。
    """

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    crawler.contents["https://taifex/attach/stock.csv"] = None
    updater.update_history()

    assert updater.dao.count_rows(FuturesMarginDAO.RATE_TABLE_NAME) == 0

    crawler.contents["https://taifex/attach/stock.csv"] = ANNOUNCEMENT_RATE_CSV
    crawler.downloaded_urls.clear()
    updater.update_history()

    assert crawler.downloaded_urls == ["https://taifex/attach/stock.csv"]
    assert updater.dao.count_rows(FuturesMarginDAO.RATE_TABLE_NAME) == 2


def test_record_is_ignored_when_the_database_lost_the_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """處理紀錄說已入庫、但資料庫沒有那個生效日（被還原或重建）時要重抓"""

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    updater.update_history()

    updater.conn.execute(f"DELETE FROM {FuturesMarginDAO.TABLE_NAME}")
    updater.conn.commit()
    crawler.downloaded_urls.clear()
    updater.update_history()

    assert crawler.downloaded_urls == ["https://taifex/attach/index.csv"]
    assert updater.dao.count_rows() == 2


def test_force_backfill_downloads_everything_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`force=True` 忽略處理紀錄全部重抓（站方更正附件內容時用）"""

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    updater.update_history()

    crawler.downloaded_urls.clear()
    updater.update_history(force=True)

    assert len(crawler.downloaded_urls) == 3


def test_processed_announcements_still_take_part_in_shared_url_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    已處理的公告不重開明細頁，但仍要參與「共用同一個網址」的判斷

    只解析新公告時，較早的新公告若與較晚、已處理的公告共用網址，
    會被當成專屬檔名而下載到被覆寫過的內容。
    """

    updater, crawler = make_history_updater(tmp_path, monkeypatch)
    announcements: List[Dict[str, str]] = [
        {"date": "2022/04/14", "link": "old", "title": "t"},
        {"date": "2026/03/31", "link": "new", "title": "t"},
    ]

    resolved: List[Tuple[Dict[str, str], Optional[str]]] = updater.resolve_csv_urls(
        announcements, known_urls={"new": "https://taifex/attach/old.csv"}
    )

    assert crawler.resolved_links == ["old"]
    assert resolved[0][1] is None
    assert resolved[1][1] == "https://taifex/attach/old.csv"
