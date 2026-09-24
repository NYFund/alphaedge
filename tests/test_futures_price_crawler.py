import datetime

import pandas as pd
import pytest

from core.pipeline.tw.crawlers.futures_price_crawler import FuturesPriceCrawler
from core.utils import FuturesSession

"""
台期貨行情爬蟲的純函式測試：表單組裝、挑表、商品防呆

**完全不連網路**——爬蟲最容易壞的三處（form 欄位漏帶、誤取價差表、到期月份被
轉成 float）都不需要真的送出請求就能驗，故以固定 HTML fixture 覆蓋。
"""

DATE: datetime.date = datetime.date(2026, 8, 27)

# 真實回應的結構縮影：第一張為行情表（單層欄位），第二張為「價差對價差成交」
# （MultiIndex 欄位）。挑錯表不會報錯，只會讓整批資料無聲走樣，故必須測。
QUOTE_HTML: str = """
<table>
  <tr><th>契約</th><th>到期月份</th><th>開盤價</th><th>結算價</th></tr>
  <tr><td>TX</td><td>202609</td><td>46175</td><td>46064</td></tr>
  <tr><td>TX</td><td>202610</td><td>46303</td><td>46246</td></tr>
</table>
<table>
  <tr><th colspan="3">價差對價差成交</th></tr>
  <tr><th>契約</th><th>到期月份</th><th>開盤價</th></tr>
  <tr><td>TX</td><td>202609/202610</td><td>-170</td></tr>
</table>
"""


# === 表單組裝 ===
def test_form_data_carries_both_commodity_fields() -> None:
    """商品代碼須同時帶 commodity_id 與 commodity_idt，只給一個不會生效"""

    form = FuturesPriceCrawler.build_form_data(DATE, "TX", FuturesSession.DAY)

    assert form["commodity_id"] == "TX"
    assert form["commodity_idt"] == "TX"


def test_form_data_uses_slash_date_format() -> None:
    """TAIFEX 的 queryDate 是 yyyy/MM/dd，不是 yyyyMMdd"""

    form = FuturesPriceCrawler.build_form_data(DATE, "TX", FuturesSession.DAY)

    assert form["queryDate"] == "2026/08/27"


def test_session_maps_to_market_code() -> None:
    """日盤 0、夜盤 1；大小寫兩個欄位都要帶，站方兩個都會讀"""

    day = FuturesPriceCrawler.build_form_data(DATE, "TX", FuturesSession.DAY)
    night = FuturesPriceCrawler.build_form_data(DATE, "TX", FuturesSession.NIGHT)

    assert day["MarketCode"] == day["marketCode"] == "0"
    assert night["MarketCode"] == night["marketCode"] == "1"


# === 挑表 ===
def test_picks_quote_table_not_spread_table() -> None:
    """頁面同時有行情表與「價差對價差成交」表，必須取前者"""

    df = FuturesPriceCrawler.extract_quote_table(QUOTE_HTML)

    assert df is not None
    assert "結算價" in df.columns  # 價差表沒有結算價
    assert len(df) == 2


def test_expiry_month_stays_string() -> None:
    """
    到期月份不可被推斷成 float

    月契約 `202609` 全為數字，未指定 converters 時會變成 `202609.0`，
    主鍵直接走樣；而週契約 `202609W1` 又必須是字串，兩者只能都當字串。
    """

    df = FuturesPriceCrawler.extract_quote_table(QUOTE_HTML)

    assert list(df["到期月份"]) == ["202609", "202610"]
    # 逐一確認型別，不比對 dtype——pandas 版本間 object／StringDtype 會變
    assert all(isinstance(v, str) for v in df["到期月份"])


def test_returns_none_when_page_has_no_table() -> None:
    """
    頁面沒有任何表格時須回傳 None 而非拋錯

    此情境下 pandas 會一路退到 html5lib，未安裝時拋的是 `ModuleNotFoundError`
    而非 `ValueError`——只 catch ValueError 會讓爬蟲在假日整個炸掉，故一併釘住。
    """

    assert (
        FuturesPriceCrawler.extract_quote_table("<html><body>無資料</body></html>")
        is None
    )


def test_returns_none_when_only_spread_table_present() -> None:
    """只有價差表時視同無資料，不可退而求其次取它"""

    spread_only: str = QUOTE_HTML.split("<table>")[2]

    df = FuturesPriceCrawler.extract_quote_table("<table>" + spread_only)

    assert df is None or "結算價" not in getattr(df, "columns", [])


# === 商品防呆 ===
def test_malformed_product_raises() -> None:
    """明顯不合法的字串要在送出請求前擋下"""

    for bad in ("", "tx", "T", "TX-1", "A" * 11):
        with pytest.raises(ValueError, match="格式不正確"):
            FuturesPriceCrawler.validate_product(bad)


def test_stock_and_etf_futures_are_allowed() -> None:
    """
    股票期貨與 ETF 期貨不可被擋

    它們不在 `FuturesProduct`（只收 15 檔臺股指數期貨）內，但實測
    `CDF`（台積電期）、`NYF`（0050 期貨）走 `commodity_id` 都能正常取得行情，
    而它們本來就是爬取目標（`FuturesPriceUpdater.update_stock_futures()`）。
    拿 Enum 當白名單會把它們一起擋掉。
    """

    FuturesPriceCrawler.validate_product("CDF")
    FuturesPriceCrawler.validate_product("NYF")
    FuturesPriceCrawler.validate_product("EEF")


def test_known_product_passes() -> None:
    """FuturesProduct 內的代碼一律放行"""

    FuturesPriceCrawler.validate_product("TX")
    FuturesPriceCrawler.validate_product("MTX")


def test_configured_targets_are_all_valid() -> None:
    """設定檔裡的商品必須全部通過防呆——這條擋的是改壞 config 而非改壞爬蟲"""

    from core.config import FUTURES_TARGET_PRODUCTS

    for product in FUTURES_TARGET_PRODUCTS:
        FuturesPriceCrawler.validate_product(product)


def test_quote_table_key_column_matches_real_header() -> None:
    """
    挑表用的欄位名須與真實表頭一致

    真實表頭是「契約」；若 TAIFEX 改名而此常數沒跟著改，`extract_quote_table`
    會一路回傳 None，看起來就像「每天都是假日」——靜默失敗，故釘住。
    """

    df: pd.DataFrame = pd.read_html(
        __import__("io").StringIO(QUOTE_HTML), converters={0: str, 1: str}
    )[0]

    assert FuturesPriceCrawler.QUOTE_TABLE_KEY_COLUMN in df.columns
