import datetime
import re
from io import StringIO
from typing import Dict, List, Optional, Set

import pandas as pd
import requests
from loguru import logger

from core.config import FUTURES_TARGET_PRODUCTS
from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.shared.request_utils import RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import FuturesProduct, FuturesSession, TimeUtils

"""
台期貨每日交易行情爬蟲（TAIFEX）

**與 TWSE／TPEX 各爬蟲最大的差異：這是 POST 端點，參數走 form data**。
把日期塞進 query string 不會報錯，但完全不生效，會一直拿到最新交易日的資料
（2026-08-29 實測），故一律以 `RequestUtils.requests_post()` 送 form。

1. 資料起點
    - TX 臺股期貨實測最早為 **1998-07-21**（該商品上市日），逐日確認 07/20 無資料、
      07/21 起有；亦即本來源提供完整歷史，沒有截斷。
    - 其餘商品各有上市日（MTX 2001、TE／TF 1999-07、TMF 2022 之後），
      查詢早於上市日的日期會**正常地查無資料**，不是錯誤。

2. 一次只能查一個商品
    - 商品由 `commodity_id` ／ `commodity_idt` 指定，兩者須帶相同值。
    - 因此請求數 = 商品數 × 時段數 × 交易日數；單檔 TX 全段約 13,800 次。
      節流由 updater 負責，本層不 sleep。

3. 日盤與夜盤是兩筆獨立行情，須分別查詢
    - `MarketCode=0`（日盤）17 欄，成交量拆為「盤後／一般／合計」三欄。
    - `MarketCode=1`（夜盤）15 欄，成交量僅一欄，且**結算價與未沖銷契約量皆為 `-`**
      （兩者屬日結數字，於日盤時段才產出）。
    - 兩者 OHLC 不同，並非同一份資料的兩種呈現；2026-08-27 TX 實測
      日盤收 46078、夜盤收 45993，而日盤表列的「盤後成交量」與夜盤的成交量一致。

4. 解析失敗有兩種，**不可混為一談**
    - 非交易日：頁面正常但沒有行情表，`pd.read_html` 拋 `ValueError` → 記 info。
    - 真的壞了：缺 parser 套件（`ModuleNotFoundError`）、版面改制、回應被攔截等
      → 記 warning。既有 crawler 慣例是一律 `except Exception` 當假日吞掉，
      那會讓故障被誤讀成「連續好幾個月都放假」。

5. 回傳的表格有兩張，**第二張是「價差對價差成交」**（跨月價差委託），
   欄位為 MultiIndex 且語意完全不同，誤取會讓整批資料無聲走樣。
   故本層以「是否存在單層的 `契約` 欄」挑表，不用位置索引。

6. `到期月份(週別)` 不是純數字
    - 月契約為 `202609`，週契約為 `202609W1`。未指定 converters 時 pandas 會把
      全數字的欄位推斷成 float 而變成 `202609.0`，主鍵直接走樣，
      故 `契約` 與 `到期月份` 兩欄一律以 `converters` 保留原始字串。
"""


class FuturesPriceCrawler(BaseDataCrawler):
    """爬取台期貨每日交易行情（各月份契約 OHLC、結算價、未沖銷契約量）"""

    # 交易時段 → TAIFEX 表單的 MarketCode
    MARKET_CODE: Dict[str, str] = {
        FuturesSession.DAY.value: "0",
        FuturesSession.NIGHT.value: "1",
    }

    # 行情表的識別欄位；第二張「價差對價差成交」表為 MultiIndex，不會有單層的此欄
    QUOTE_TABLE_KEY_COLUMN: str = "契約"

    # 商品代碼格式：2~10 碼大寫英數（TX、MTX、TMF、CDF、NYF…）
    PRODUCT_CODE_PATTERN: str = r"[A-Z0-9]{2,10}"

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, date: datetime.date) -> None:
        """依 `FUTURES_TARGET_PRODUCTS` 逐商品、逐時段爬取單日行情"""

        for product in FUTURES_TARGET_PRODUCTS:
            # **不可寫 `for session in FuturesSession`**：那會連整併用的
            # `COMBINED` 也一起爬，而來源根本沒有那個時段（見 `data_sessions()`）
            for session in FuturesSession.data_sessions():
                self.crawl_futures_price(date, product, session)

    def crawl_futures_price(
        self,
        date: datetime.date,
        product: str,
        session: FuturesSession,
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            單一商品、單一交易時段、單日的行情爬取

            **查無資料一律回傳 `None` 而不拋錯**，因為此處無法區分三種成因：
            非交易日、該商品當日尚未上市、站方尚未更新。三者的因應方式不同，
            判斷交給 updater（它知道回補區間與商品上市日）。
        - Parameters:
            - date: datetime.date
                查詢日
            - product: str
                商品代碼，須為 `FuturesProduct` 的成員（Ex: TX）
            - session: FuturesSession
                交易時段（日盤／夜盤）
        - Return:
            - Optional[pd.DataFrame]
                原始行情表；查無資料時為 None
        """

        self.validate_product(product)

        logger.info(
            f"* Start crawling TAIFEX futures price: {product} {session.value} {date}"
        )

        url: str = URLManager.get_url("TAIFEX_FUTURES_PRICE_URL")
        response: Optional[requests.Response] = RequestUtils.requests_post(
            url, data=self.build_form_data(date, product, session)
        )

        if response is None:
            return None

        df: Optional[pd.DataFrame] = self.extract_quote_table(response.text)

        if df is None or df.empty:
            # 非交易日／商品尚未上市／站方未更新，三者在此無法區分
            logger.info(f"{date} {product} {session.value}: no data")
            return None

        return df

    @classmethod
    def build_form_data(
        cls,
        date: datetime.date,
        product: str,
        session: FuturesSession,
    ) -> Dict[str, str]:
        """
        - Description:
            組出 TAIFEX 查詢表單的 form data

            欄位名稱取自該頁表單本身（2026-08-29 實查）。空字串欄位看似多餘，
            但表單送出時本來就會帶，缺漏時的行為未經驗證，故原樣保留。
        - Parameters:
            - date: datetime.date
                查詢日
            - product: str
                商品代碼
            - session: FuturesSession
                交易時段
        - Return:
            - Dict[str, str]
                可直接餵給 `requests.post(data=...)` 的表單內容
        """

        market_code: str = cls.MARKET_CODE[session.value]

        return {
            "queryType": "2",
            "marketCode": market_code,
            "MarketCode": market_code,
            "dateaddcnt": "",
            # 商品代碼要同時帶這兩個欄位，只給其中一個不會生效
            "commodity_id": product,
            "commodity_idt": product,
            # 以下三個為 ETF／股票期貨的下拉，查指數期貨時留空
            "commodity_id2": "",
            "commodity_id2t": "",
            "commodity_id2t2": "",
            "queryDate": TimeUtils.format_date(date, sep="/"),
            "button": "送出查詢",
        }

    @classmethod
    def extract_quote_table(cls, html: str) -> Optional[pd.DataFrame]:
        """
        - Description:
            自回應 HTML 取出行情表

            **不用位置索引挑表**：頁面會一併回傳「價差對價差成交」表，
            欄位為 MultiIndex、語意完全不同，一旦 TAIFEX 調整表格順序，
            用索引會無聲取到錯的那張。改以單層 `契約` 欄位辨識。

            `契約` 與 `到期月份` 以 converters 保留字串：週契約為 `202609W1`，
            而全數字的月契約會被 pandas 推斷成 float 而變成 `202609.0`。
        - Parameters:
            - html: str
                回應內容
        - Return:
            - Optional[pd.DataFrame]
                行情表；解析不出或頁面無表格時為 None
        """

        try:
            tables: List[pd.DataFrame] = pd.read_html(
                StringIO(html), converters={0: str, 1: str}
            )
        except ValueError:
            # 非交易日：頁面仍是正常 HTML，但沒有行情表，pandas 拋 ValueError。
            # 這是預期內的情況，用 info 不用 warning，否則整段回補會被假日洗版
            logger.info("[Futures Price] 頁面無表格（非交易日或站方未更新）")
            return None
        except Exception as error:
            # **不可與上面合併，也不可收斂成具名例外**：解析器缺套件、版面改制、
            # 回應被攔截等都會走到這裡，它們是真的壞了，不是「今天沒開盤」。
            # 既有 crawler 慣例是一律當假日吞掉，那會讓故障被誤讀為長假。
            # 收斂成列舉的話，漏掉的那一種會讓整段歷史回補中止而不是少一天
            logger.warning(
                f"[Futures Price] 解析行情頁失敗：{type(error).__name__}: {error}"
            )
            return None

        for table in tables:
            if cls.QUOTE_TABLE_KEY_COLUMN in table.columns:
                return table

        return None

    @classmethod
    def validate_product(cls, product: str) -> None:
        """
        - Description:
            商品代碼的**格式**防呆；不做白名單比對

            `FUTURES_TARGET_PRODUCTS` 是手寫字面值，拼錯不會有編譯期提示，
            而一次回補動輒數千次請求。但「哪些代碼合法」沒有可靠的靜態答案：

            1. **不能用 `FuturesProduct`**：它只收 15 檔臺股指數期貨，拿來當白名單
               會連帶擋掉股票期貨（295 檔）與 ETF 期貨（24 檔）——那些本來就要爬，
               只是排在 Phase6。實測 `CDF`（台積電期）、`NYF`（0050 期貨）走
               `commodity_id` 都能正常取得行情。`FuturesProduct` 的職責是承載
               契約乘數，不是限制爬取範圍。
            2. **不能抓 TAIFEX 下拉當白名單**：該頁下拉的內容**不穩定**——
               2026-08-29 同一天內兩次抓取分別得到 32／319／319 與 26／7／7 個選項，
               且商品集合不同（後者有 CDF、NYF，卻少了 BTF、E4F、GTF、XIF）。
               以它為準會隨執行時間隨機拒絕合法商品。

            因此本層只擋明顯不合法的字串；「代碼有效但查不到資料」由
            `FuturesPriceUpdater` 的空產出保險絲負責（它直接對應真正的失效模式：
            整段回補都沒有資料），見該類的 `EMPTY_PRODUCT_ABORT_THRESHOLD`。
        - Parameters:
            - product: str
                待檢核的商品代碼
        """

        if not product or not re.fullmatch(cls.PRODUCT_CODE_PATTERN, product):
            raise ValueError(
                f"期貨商品代碼格式不正確：{product!r}。"
                f"應為 2~10 碼大寫英數（Ex: TX、MTX、CDF）"
            )

        known: Set[str] = {member.value for member in FuturesProduct}
        if product not in known:
            # 股票期貨與 ETF 期貨本來就不在 FuturesProduct 內，屬預期內
            logger.info(
                f"[Futures Price] {product} 不在 FuturesProduct（臺股指數期貨）清單內；"
                f"若為股票期貨或 ETF 期貨屬正常，但其契約乘數尚未登錄"
            )
