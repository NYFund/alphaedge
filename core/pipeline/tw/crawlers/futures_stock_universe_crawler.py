import re
from io import StringIO
from typing import List, Optional

import pandas as pd
import requests
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.shared.request_utils import RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager

"""
股票期貨標的池爬蟲（TAIFEX 股票期貨、選擇權標的證券一覽表）

**與 `futures_price_crawler` 的定位差異：這裡爬的是「有哪些商品」，不是「行情」**。
行情頁一次只能查一個商品，而要查哪些商品正是本層要回答的問題；把清單寫死在
`FUTURES_TARGET_PRODUCTS` 對 320 檔且會隨掛牌／下市異動的股期並不可行。

1. 這是 GET 頁面，不是行情頁的 POST 端點
    整份清單一次回傳，不帶任何查詢參數，故一次請求即可。

2. **回傳的是「當下快照」，沒有掛牌日／下市日欄位**
    來源只告訴你「現在有這些」，不告訴你「何時開始有」。因此掛牌／下市一律由
    **快照序列的差分**推得（見 `futures_stock_universe_loader` 的 schema 說明），
    本層不做任何推論，也不要為了湊出掛牌日去猜。

3. **`NA` 會被 pandas 讀成 NaN**（2026-08-29 實測踩到）
    穩懋（3105）的商品代碼就是 `NA`，落在 pandas 預設的 NA 字面值裡，
    不關掉 `keep_default_na` 會讓這一檔無聲消失——不會報錯，只會少一檔。
    故一律 `keep_default_na=False`。

4. **證券代號必須當字串**
    ETF 標的有 `0050`（前導 0 會被吃掉）與 `00679B`（含英文字母），
    兩者只要被推斷成數字就對不回現股，故以 converters 鎖成字串。

5. 一個標的可能出現多列
    標準型與小型是**兩個獨立商品、各有自己的代碼**（台積電 CD／QF、
    世紀鋼 RU／SW），不是同一列的兩種呈現。以商品代碼為主鍵，不是證券代號。

6. 商品代碼與行情頁 `commodity_id` 的關係
    本頁給的是 2 碼代碼（`CD`），行情頁要帶的是**加尾碼 `F`** 的 `CDF`。
    2026-08-29 以 CDF／QFF／NYF／SRF／SWF／NAF 逐一實測，四種商品類型皆可正常取得行情。
    ⚠️ 除權息調整後另掛的契約（`EE1` 等數字尾碼）**不在本頁**，須走 TAIFEX
    契約調整公告，目前未接。
"""


class FuturesStockUniverseCrawler(BaseDataCrawler):
    """爬取股票期貨／ETF 期貨的標的證券一覽表"""

    # 清單表的識別字串；頁面另有一張「類型篩選」表（2 欄），不含此欄位
    UNIVERSE_TABLE_KEY_COLUMN: str = "證券代號"

    # 商品代碼欄與證券代號欄的位置；兩者都必須保留字串
    PRODUCT_CODE_COL_INDEX: int = 0
    STOCK_ID_COL_INDEX: int = 2

    # 合法的 2 碼商品代碼；用於濾掉最後一列的「標的合計數」小計列
    BASE_CODE_PATTERN: str = r"[A-Z0-9]{2}"

    # 清單頁的 2 碼代碼 → 行情頁 `commodity_id` 的尾碼
    PRODUCT_ID_SUFFIX: str = "F"

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self) -> Optional[pd.DataFrame]:
        """爬取標的證券一覽表（整份清單一次回傳，不需逐日／逐商品）"""

        return self.crawl_stock_universe()

    def crawl_stock_universe(self) -> Optional[pd.DataFrame]:
        """
        - Description:
            取得 TAIFEX 股票期貨、選擇權標的證券一覽表的原始表格

            **查不到一律回傳 None 而不拋錯**，與 `futures_price_crawler` 一致：
            站方改版、暫時無回應等成因在此層分不出來，交由 updater 判斷。
        - Return:
            - Optional[pd.DataFrame]
                原始清單表；解析失敗時為 None
        """

        logger.info("* Start crawling TAIFEX stock futures universe")

        url: str = URLManager.get_url("TAIFEX_STOCK_FUTURES_LIST_URL")
        response: Optional[requests.Response] = RequestUtils.requests_get(url)

        if response is None:
            logger.warning("[Futures Universe] 取得標的清單頁失敗")
            return None

        return self.extract_universe_table(response.text)

    @classmethod
    def extract_universe_table(cls, html: str) -> Optional[pd.DataFrame]:
        """
        - Description:
            自回應 HTML 取出標的清單表

            `keep_default_na=False` 與 converters 兩者缺一不可，理由見本檔說明
            第 3、4 點——少了任何一個都是**無聲**的資料損壞。
        - Parameters:
            - html: str
                回應內容
        - Return:
            - Optional[pd.DataFrame]
                清單表；頁面無此表或解析失敗時為 None
        """

        try:
            # **`match` 不是效能考量，是必要的**：頁面另有一張 2 欄的「類型篩選」表，
            # 而 converters 以欄位位置指定；pandas 解析那張表時會對第 2 欄取值而
            # 拋 IndexError，整份清單就一列都拿不到（2026-08-29 實測）
            tables: List[pd.DataFrame] = pd.read_html(
                StringIO(html),
                match=cls.UNIVERSE_TABLE_KEY_COLUMN,
                converters={
                    cls.PRODUCT_CODE_COL_INDEX: str,
                    cls.STOCK_ID_COL_INDEX: str,
                },
                keep_default_na=False,
            )
        except ValueError:
            # 頁面正常但沒有符合的表格：多半是站方改版或回應被攔截。
            # 與行情頁不同，這裡**沒有「非交易日」這種正常的無資料情境**，
            # 標的清單任何一天都該查得到，故一律當異常記 warning
            logger.warning("[Futures Universe] 頁面無表格（站方改版或回應被攔截）")
            return None
        except Exception as error:
            # **這層盲捕不可收斂**：上面的 `ValueError` 已經把「沒有表格」分出去了，
            # 這裡要接的是「其餘任何解析失敗」——解析器缺套件、編碼壞掉、
            # 回應被攔截成別的格式。列舉得出來的那幾種正是會漏的那幾種，
            # 而漏掉一種會讓整批回補中止，不是少一天
            logger.warning(
                f"[Futures Universe] 解析標的清單頁失敗：{type(error).__name__}: {error}"
            )
            return None

        for table in tables:
            if cls.UNIVERSE_TABLE_KEY_COLUMN in [str(col) for col in table.columns]:
                return table

        logger.warning("[Futures Universe] 找不到標的清單表（欄位結構已變動）")
        return None

    @classmethod
    def to_commodity_id(cls, base_code: str) -> str:
        """
        - Description:
            把清單頁的 2 碼商品代碼轉成行情頁要帶的 `commodity_id`

            尾碼固定為 `F`（`CD` → `CDF`），四種商品類型（個股／小型個股／
            ETF／小型 ETF）2026-08-29 皆實測可用。
        - Parameters:
            - base_code: str
                清單頁的 2 碼商品代碼
        - Return:
            - str
                行情頁的商品代碼
        """

        return f"{base_code}{cls.PRODUCT_ID_SUFFIX}"

    @classmethod
    def is_valid_base_code(cls, base_code: str) -> bool:
        """判斷是否為合法的 2 碼商品代碼（用於濾掉小計、說明列）"""

        return bool(re.fullmatch(cls.BASE_CODE_PATTERN, str(base_code).strip()))
