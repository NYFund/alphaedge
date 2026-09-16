import datetime
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from core.config import FUTURES_STOCK_UNIVERSE_TABLE_NAME
from core.dao.tw.futures_stock_universe_dao import FuturesStockUniverseDAO
from core.pipeline.tw.cleaners.futures_stock_universe_cleaner import (
    FuturesStockUniverseCleaner,
)
from core.pipeline.tw.crawlers.futures_stock_universe_crawler import (
    FuturesStockUniverseCrawler,
)
from core.utils import StockFuturesType

"""
股票期貨標的池的純函式測試：挑表、字串保留、清洗與類型判定

**完全不連網路**。本來源真正會出事的地方都不在網路層，而在解析：`NA` 被當成
NaN、`0050` 前導 0 被吃掉、小計列混進資料、契約單位對不到已知類型——四者
都不會報錯，只會讓資料無聲少一檔或走樣，故以固定 HTML fixture 覆蓋。
"""

SNAPSHOT_DATE: datetime.date = datetime.date(2026, 8, 29)

# 真實頁面的結構縮影：
# 第一張是 2 欄的「類型篩選」表——它的存在正是 crawler 必須帶 `match` 的原因
# （converters 以位置指定，解析 2 欄表時會拋 IndexError）。
# 第二張是 14 欄的標的清單，含四種商品類型、`NA` 代碼、ETF 代號與最後的小計列。
UNIVERSE_HTML: str = """
<table>
  <tr><td>類型：</td><td>全部 個股期貨(2,000股) 小型個股期貨(100股)</td></tr>
</table>
<table>
  <tr>
    <th>股票期貨、 選擇權 商品代碼</th><th>標的證券</th><th>證券代號</th>
    <th>標的證券 簡稱</th><th>是否為 股票期貨 標的</th><th>是否為 股票選擇權 標的</th>
    <th>是否為 股票選擇權週契約 標的</th><th>上市普通股 標的證券</th>
    <th>上櫃普通股 標的證券</th><th>上市ETF 標的證券</th><th>上櫃ETF 標的證券</th>
    <th>標準型證 券股數/ 受益權單位</th><th>一般交易時段 交易時間</th>
    <th>盤後交易時段 交易時間</th>
  </tr>
  <tr>
    <td>CD</td><td>台灣積體電路製造股份有限公司</td><td>2330</td><td>台積電</td>
    <td>●</td><td>●</td><td>●</td><td>◎</td><td></td><td></td><td></td>
    <td>2000</td><td>8:45~13:45</td><td>17:25~次日05:00</td>
  </tr>
  <tr>
    <td>QF</td><td>台灣積體電路製造股份有限公司</td><td>2330</td><td>台積電</td>
    <td>●</td><td></td><td></td><td>◎</td><td></td><td></td><td></td>
    <td>100</td><td>8:45~13:45</td><td>17:25~次日05:00</td>
  </tr>
  <tr>
    <td>NA</td><td>穩懋半導體股份有限公司</td><td>3105</td><td>穩懋</td>
    <td>●</td><td></td><td></td><td></td><td>◎</td><td></td><td></td>
    <td>2000</td><td>8:45~13:45</td><td>-</td>
  </tr>
  <tr>
    <td>NY</td><td>元大台灣卓越50證券投資信託基金</td><td>0050</td><td>元大台灣50ETF</td>
    <td>●</td><td></td><td></td><td></td><td></td><td>◎</td><td></td>
    <td>10000</td><td>8:45~13:45</td><td>17:25~次日05:00</td>
  </tr>
  <tr>
    <td>SR</td><td>元大台灣卓越50證券投資信託基金</td><td>0050</td><td>元大台灣50ETF</td>
    <td>●</td><td></td><td></td><td></td><td></td><td>◎</td><td></td>
    <td>1000</td><td>8:45~13:45</td><td>17:25~次日05:00</td>
  </tr>
  <tr>
    <td></td><td>標的合計數：</td><td></td><td></td><td>5</td><td>1</td><td>1</td>
    <td>2</td><td>1</td><td>2</td><td>0</td><td></td><td></td><td></td>
  </tr>
</table>
"""


@pytest.fixture(autouse=True)
def isolate_downloads_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    把 cleaner 的落地目錄導到暫存區

    cleaner 清完就把結果寫成 CSV，預設落在真實的
    `data/downloads/tw_futures/universe/`——本檔的 fixture 只有 5 列，
    跑一次測試就會把同名的真實快照整份覆蓋掉，且不會有任何徵兆。
    """

    monkeypatch.setattr(
        "core.pipeline.tw.cleaners.futures_stock_universe_cleaner."
        "FUTURES_UNIVERSE_DOWNLOADS_PATH",
        tmp_path / "universe",
    )


def crawl_and_clean() -> pd.DataFrame:
    """走一次真實的 crawler → cleaner 路徑，回傳清洗後的快照"""

    raw_df: pd.DataFrame = FuturesStockUniverseCrawler.extract_universe_table(
        UNIVERSE_HTML
    )
    return FuturesStockUniverseCleaner().clean_stock_universe(raw_df, SNAPSHOT_DATE)


# === 挑表 ===
def test_picks_universe_table_not_filter_table() -> None:
    """頁面第一張是 2 欄的類型篩選表，必須取後面的清單表"""

    df = FuturesStockUniverseCrawler.extract_universe_table(UNIVERSE_HTML)

    assert df is not None
    assert df.shape[1] == 14
    assert len(df) == 6  # 5 檔商品 ＋ 1 列小計


def test_returns_none_when_page_has_no_table() -> None:
    """頁面沒有清單表時回傳 None 而非拋錯（站方改版時的表現）"""

    assert (
        FuturesStockUniverseCrawler.extract_universe_table(
            "<html><body>維護中</body></html>"
        )
        is None
    )


# === 字串保留 ===
def test_na_product_code_is_not_parsed_as_nan() -> None:
    """
    `NA` 是穩懋的商品代碼，不是缺值

    它落在 pandas 預設的 NA 字面值裡；沒關掉 `keep_default_na` 這一檔會無聲消失
    ——不報錯、不警告，只是少一檔。
    """

    df = FuturesStockUniverseCrawler.extract_universe_table(UNIVERSE_HTML)
    codes = list(df.iloc[:, FuturesStockUniverseCrawler.PRODUCT_CODE_COL_INDEX])

    assert "NA" in codes


def test_etf_stock_id_keeps_leading_zeros() -> None:
    """`0050` 被推斷成數字就對不回現股，故必須是字串"""

    df = FuturesStockUniverseCrawler.extract_universe_table(UNIVERSE_HTML)
    stock_ids = list(df.iloc[:, FuturesStockUniverseCrawler.STOCK_ID_COL_INDEX])

    assert "0050" in stock_ids
    assert all(isinstance(v, str) for v in stock_ids)


# === 商品代碼 ===
def test_commodity_id_appends_f_suffix() -> None:
    """清單頁給 2 碼代碼，行情頁要帶的是加尾碼 F 的版本"""

    assert FuturesStockUniverseCrawler.to_commodity_id("CD") == "CDF"
    assert FuturesStockUniverseCrawler.to_commodity_id("NA") == "NAF"


def test_only_two_char_codes_are_valid() -> None:
    """小計列的空代碼與說明文字要被擋下"""

    assert FuturesStockUniverseCrawler.is_valid_base_code("CD")
    assert not FuturesStockUniverseCrawler.is_valid_base_code("")
    assert not FuturesStockUniverseCrawler.is_valid_base_code("標的合計數：")
    assert not FuturesStockUniverseCrawler.is_valid_base_code("CDF")


# === 清洗 ===
def test_subtotal_row_is_dropped() -> None:
    """最後一列的「標的合計數」不是商品，必須濾掉"""

    df = crawl_and_clean()

    assert len(df) == 5
    assert set(df["product_id"]) == {"CDF", "QFF", "NAF", "NYF", "SRF"}


def test_product_type_inferred_from_contract_size() -> None:
    """
    來源沒有類型欄位，類型由「標準型證券股數／受益權單位」反推

    四種數量彼此不重疊（2000／100／10000／1000），故不會誤判。
    """

    df = crawl_and_clean().set_index("product_id")

    assert df.loc["CDF", "product_type"] == StockFuturesType.SINGLE.value
    assert df.loc["QFF", "product_type"] == StockFuturesType.MINI_SINGLE.value
    assert df.loc["NYF", "product_type"] == StockFuturesType.ETF.value
    assert df.loc["SRF", "product_type"] == StockFuturesType.MINI_ETF.value


def test_same_underlying_can_have_multiple_products() -> None:
    """
    台積電有標準型與小型兩個商品，兩者代碼不同

    以證券代號當主鍵會讓其中一個被覆蓋掉，故主鍵必須是商品代碼。
    """

    df = crawl_and_clean()
    tsmc = df[df["underlying_stock_id"] == "2330"]

    assert len(tsmc) == 2
    assert set(tsmc["product_id"]) == {"CDF", "QFF"}


def test_missing_night_session_becomes_null() -> None:
    """
    沒有夜盤的商品其欄位為 `-`，須清成 NULL

    填空字串會讓「沒有夜盤」與「夜盤時段未知」混為一談。
    """

    df = crawl_and_clean().set_index("product_id")

    assert pd.isna(df.loc["NAF", "night_session_time"])
    assert df.loc["CDF", "night_session_time"] == "17:25~次日05:00"


def test_listing_board_resolved_from_marker_columns() -> None:
    """上市／上櫃由四個互斥的標記欄位判定"""

    df = crawl_and_clean().set_index("product_id")

    assert df.loc["CDF", "underlying_listing_board"] == "上市"
    assert df.loc["NAF", "underlying_listing_board"] == "上櫃"
    assert df.loc["NYF", "underlying_listing_board"] == "上市"


def test_unknown_contract_size_aborts_the_batch() -> None:
    """
    出現沒登錄過的契約單位時整批中止

    那代表 TAIFEX 新增了商品類型；靜靜歸到某個既有類型會讓下游拿錯契約單位，
    比中斷難查得多。
    """

    html: str = UNIVERSE_HTML.replace("<td>2000</td>", "<td>5000</td>", 1)
    raw_df = FuturesStockUniverseCrawler.extract_universe_table(html)

    assert (
        FuturesStockUniverseCleaner().clean_stock_universe(raw_df, SNAPSHOT_DATE)
        is None
    )


def test_column_count_mismatch_aborts_the_batch() -> None:
    """欄位數不符代表來源改制，寧可不入庫也不要錯位"""

    raw_df: pd.DataFrame = pd.DataFrame({"a": ["CD"], "b": ["台積電"]})

    assert (
        FuturesStockUniverseCleaner().clean_stock_universe(raw_df, SNAPSHOT_DATE)
        is None
    )


def test_cleaned_columns_match_table_schema() -> None:
    """
    清洗後的欄位須與資料表宣告完全一致（含順序）

    `insert_or_ignore` 以欄位名組 SQL，少一欄不會報錯（NOT NULL 才會），
    多一欄或錯字則整批失敗。真正危險的是**順序以外的漂移**：cleaner 加了欄位
    卻忘了改 schema 時，錯誤要到入庫才浮出來，故在這裡就釘住。

    建表走記憶體 DB，不碰真正的 tw_futures.db。
    """

    dao: FuturesStockUniverseDAO = FuturesStockUniverseDAO(
        conn=sqlite3.connect(":memory:")
    )
    dao.create_table()

    table_cols = [
        row[1]
        for row in dao.conn.execute(
            f"PRAGMA table_info('{FUTURES_STOCK_UNIVERSE_TABLE_NAME}')"
        )
    ]
    dao.conn.close()

    assert FuturesStockUniverseCleaner().universe_cleaned_cols == table_cols
    assert list(crawl_and_clean().columns) == table_cols
