import datetime
from typing import Dict, Optional

import requests
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.shared.request_utils import RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import FileEncoding

"""
台期貨籌碼爬蟲（三大法人、大額交易人、選擇權 PCR）

**三個資料集共用同一種請求形態**：POST ＋ 日期區間，回 big5 CSV。
故合成一支 crawler，而不是三支只差網址的檔案。

1. **一次請求涵蓋所有商品，而且支援日期區間**
    與 `futures_price_crawler` 的「逐商品 × 逐時段 × 逐日」完全不同——
    這三個端點一次請求就拿到全市場的**一整段期間**。
    **不要逐日打**：2015 年以來逐日是 4,262 次請求，改用月批次只要 140 次，
    而 TAIFEX 擋流量時回的是 HTTP 200 ＋ HTML（見下方第 4 點），
    請求數愈多被擋的機率愈高。

    各端點的區間上限實測（2026-09-02）：三大法人 1 年可、大額交易人 3 個月可
    但 1 年不行、PCR 1 個月可但 3 個月不行。故一律用**月批次**，最保守也最一致。

2. **編碼是 big5**
    與保證金 crawler 同一個處境：`requests` 猜的編碼不可信，故本層一律回傳
    **解碼後的字串**，解碼點只有這裡一處。

3. **盤後才有資料**
    三者都在收盤後公布。當日盤中查會拿到空表（只有表頭），那是正常狀態，
    不是失敗——回測要用的本來就是前一交易日的籌碼（見 `FuturesChipAPI`）。

4. **⚠️ 被擋流量與「真的沒資料」長得一模一樣**
    TAIFEX 擋流量時回 **HTTP 200 ＋ 一整頁 HTML**，與非交易日的回應完全相同。
    2026-09-02 的歷史回補就是這樣：逐日打了 4,000 多次之後被擋，
    2024-08 ~ 2025-10 約 250 個交易日**全部被記成「查無資料」**，
    事後單獨重查每一天都有資料。故本層只負責回報「拿到的是不是 CSV」，
    **判斷該不該重試是 updater 的責任**（它知道那天是不是交易日）。

5. **三大法人只有約兩年的歷史**（2026-09-02 實測，切點在 2024-08-17~19）
    更早的日期無論用哪一組參數、換哪一個端點都拿不到——查詢頁甚至會**靜靜
    回傳最新一天的資料**而不是報錯。這是來源限制，不是爬蟲問題。

6. **查不到一律回 None 而不拋錯**
    站方改版、假日、暫時無回應在此層分不出來，交由 updater 判斷。
"""


class FuturesChipCrawler(BaseDataCrawler):
    """爬取 TAIFEX 的三大法人、大額交易人與選擇權 PCR"""

    # 三個端點的日期參數名不同，統一在此對照
    DATE_PARAM_KEYS: Dict[str, tuple] = {
        "TAIFEX_FUTURES_INSTITUTIONAL_URL": ("queryStartDate", "queryEndDate"),
        "TAIFEX_FUTURES_LARGE_TRADER_URL": ("queryStartDate", "queryEndDate"),
        "TAIFEX_FUTURES_PCR_URL": ("queryStartDate", "queryEndDate"),
    }

    # 只有表頭沒有資料列時的行數門檻（表頭一行 ＋ 可能的空行）
    MIN_DATA_LINES: int = 2

    # CSV 表頭的識別字：三個端點的第一欄都是「日期」。
    #
    # ⚠️ **非交易日回的是 HTTP 200 ＋ 一整頁 HTML**（不是空 CSV、也不是 404）。
    # 2026-09-02 實測：查週日拿到 19 行的 HTML，行數檢查完全擋不住它，
    # 下游 `csv` 解析出來會是一堆亂七八糟的「欄位」而不會報錯。
    # 故一律檢查第一行是不是真的 CSV 表頭——這是本端點最容易靜默寫錯資料的地方。
    CSV_HEADER_KEYWORD: str = "日期"

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self, date: datetime.date) -> Optional[str]:
        """預設路徑：爬三大法人（其餘兩個資料集各有專屬方法）"""

        return self.crawl_institutional(date)

    def crawl_institutional(
        self, start_date: datetime.date, end_date: Optional[datetime.date] = None
    ) -> Optional[str]:
        """三大法人（自營商／投信／外資）的逐商品多空口數與未平倉"""

        return self.fetch_csv(
            "TAIFEX_FUTURES_INSTITUTIONAL_URL", start_date, end_date, "三大法人"
        )

    def crawl_large_trader(
        self, start_date: datetime.date, end_date: Optional[datetime.date] = None
    ) -> Optional[str]:
        """大額交易人：前五大／前十大的買賣方部位，含特定法人拆分"""

        return self.fetch_csv(
            "TAIFEX_FUTURES_LARGE_TRADER_URL", start_date, end_date, "大額交易人"
        )

    def crawl_put_call_ratio(
        self, start_date: datetime.date, end_date: Optional[datetime.date] = None
    ) -> Optional[str]:
        """選擇權 Put/Call Ratio：一天一列"""

        return self.fetch_csv("TAIFEX_FUTURES_PCR_URL", start_date, end_date, "PCR")

    def fetch_csv(
        self,
        url_name: str,
        start_date: datetime.date,
        end_date: Optional[datetime.date] = None,
        label: str = "",
    ) -> Optional[str]:
        """
        - Description:
            以日期區間查詢並取回 CSV 原文（big5 解碼）

            `end_date` 省略時等同單日查詢。**回傳 None 有兩種成因且本層分不出來**
            ——非交易日／尚未公布，或被擋流量（兩者都是 HTTP 200 ＋ HTML）。
            只有 updater 知道那天是不是交易日，故判斷該不該重試是它的責任。
        - Parameters:
            - url_name: str
                `URLManager` 的 key
            - start_date / end_date: datetime.date
                查詢區間；`end_date` 為 None 時等同 `start_date`
            - label: str
                log 用的資料集名稱
        - Return:
            - Optional[str]
                CSV 原文；不是 CSV（非交易日或被擋）時為 None
        """

        end: datetime.date = end_date or start_date
        span: str = str(start_date) if end == start_date else f"{start_date} ~ {end}"
        logger.info(f"* Start crawling TAIFEX futures chip: {label} {span}")

        url: str = URLManager.get_url(url_name)
        start_key, end_key = self.DATE_PARAM_KEYS[url_name]

        response: Optional[requests.Response] = RequestUtils.requests_post(
            url,
            data={
                start_key: start_date.strftime("%Y/%m/%d"),
                end_key: end.strftime("%Y/%m/%d"),
            },
        )
        if response is None:
            logger.warning(f"[Futures Chip] 取得{label}失敗：{span}")
            return None

        text: str = response.content.decode(FileEncoding.BIG5.value, errors="replace")
        lines: list = [line for line in text.splitlines() if line.strip()]

        # 非交易日與被擋流量回的都是 HTML（見 `CSV_HEADER_KEYWORD`）
        if not lines or self.CSV_HEADER_KEYWORD not in lines[0] or "," not in lines[0]:
            logger.info(f"{span} {label}: 沒有取得 CSV（非交易日、未公布或被擋）")
            return None

        # 只有表頭代表該區間內確實沒有資料
        if len(lines) < self.MIN_DATA_LINES:
            logger.info(f"{span} {label}: no data")
            return None

        return text
