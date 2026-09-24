import datetime
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

import requests
from loguru import logger

from core.pipeline.shared.base_crawler import BaseDataCrawler
from core.pipeline.shared.request_utils import RequestUtils
from core.pipeline.tw.utils.url_manager import URLManager
from core.utils import FileEncoding

"""
台期貨保證金爬蟲（TAIFEX 保證金一覽表）

**與本目錄其他 crawler 最大的差別：回的是 CSV 純文字，不是 HTML**。
`pd.read_html` 在這裡完全用不上，一律走 `csv` 模組（解析交給 cleaner）。

1. **編碼是 big5，不是 UTF-8**
    `requests` 猜到的編碼不可信，故本層一律回傳**解碼後的字串**而不是 `Response`，
    解碼點只有這裡一處，下游不必再操心編碼。

2. **來源只給「現在這一組」，沒有歷史**
    與 `futures_stock_universe_crawler` 同一個處境：靠快照序列累積變動歷史。
    2020/03 之後的歷史另有調整公告可補（`FuturesMarginUpdater.update_history()`），
    一覽表這條路徑不負責。

3. **第一行不是表頭**
    指數類的第一行是 `更新日期:YYYY/MM/DD`（＝這組保證金的生效日），
    股票類前面還有兩行標題文字。生效日的解析在 cleaner，本層原樣回傳。

4. **兩支端點的格式語意不同**
    指數類給「每口固定金額」、股票類給「適用比例 ＋ 級距」，
    因此下游是兩條清洗與入庫路徑。

5. **歷史調整走公告，不是一覽表**
    一覽表只有現值。歷史要先用 `crawl_announcements()` 查公告清單，
    再逐筆 `resolve_announcement_csv()` 取附件——**附件檔名每則都不同**
    （`保證金調整情形列表.csv`／`0312保證金調整.csv`／`保證金調整20260310.csv`…），
    組不出通則，一定要從明細頁上抓。
    2015~2019 的公告只有掃描 PDF（無文字層），取不到數值。
"""


class FuturesMarginCrawler(BaseDataCrawler):
    """爬取 TAIFEX 保證金一覽表（指數類與股票類）與歷史調整公告"""

    # 公告查詢的固定參數；`isQuery` 必須是 "1"，見 `crawl_announcements()`
    ANNOUNCEMENT_QUERY_FLAG: str = "1"
    ANNOUNCEMENT_NEWS_TYPE: str = "公告"

    # **關鍵字只能用最短的「保證金」，不可用更精確的多字詞**（2026-09-01 兩次實測）：
    #
    # 1. TAIFEX 的標題措辭會變：2026/04/21 起由「…之保證金**金額**」改為
    #    「…之**保證金**」，查前者會從那天起靜默漏掉每一次調整。
    # 2. **標題裡有換行造成的空白**：2024/08/21 那則實際存的是
    #    「所有月份保證金 金額」（保證金與金額之間有空白），
    #    子字串比對「保證金金額」因此比不到——而漏掉它會讓後續三則公告
    #    因為「調整前接不上」被守門連鎖拒收。
    #
    # 站方的關鍵字是子字串比對，**任何多字詞都可能被空白切斷**，
    # 故一律用最短的詞查寬，再由附件結構收斂（見下一段）。
    ANNOUNCEMENT_KEYWORD: str = "保證金"

    # **不用標題判斷這是不是保證金調整公告**，以下每一種標題規則都被實際資料打臉
    # （2026-09-01 實測）：
    #
    # | 標題規則 | 為什麼行不通 |
    # |----------|-------------|
    # | 關鍵字「保證金金額」 | 站方 2026/04 起改措辭為「之保證金」 |
    # | 關鍵字「之保證金」 | 標題有換行空白（`保證金 金額`），子字串比不到 |
    # | regex `調整.*保證金` | **春節後的公告用「回調」不是「調整」**，整批漏掉 |
    # | 再列舉「調高」「調降」 | 還有「調高…適用比例為…之1.5倍」等寫法，列不完 |
    #
    # 措辭是人寫的，永遠列不完；**附件的結構才是穩定的**。故一律取回所有
    # 提到「保證金」的公告（2020~2026 共 320 則），由下游三道結構性檢查收斂：
    # ① 附件必須是 CSV 且表頭相符 ② `契約ABC值` 非空的列（選擇權）剔除
    # ③ 同一附件網址被多則引用時只信最新那則。
    # 代價是每次全量回補多約 50 次請求，換到的是「不會因為措辭改變而靜默漏資料」。

    # 公告明細頁的相對連結基準
    NEWS_DETAIL_BASE: str = "https://www.taifex.com.tw/cht/11/"

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Crawler"""
        pass

    def crawl(self) -> Optional[str]:
        """爬取指數類保證金一覽表（預設路徑）"""

        return self.crawl_index_margin()

    def crawl_index_margin(self) -> Optional[str]:
        """
        - Description:
            取得股價指數類的現行保證金一覽表（CSV 原文）

            **查不到一律回傳 None 而不拋錯**，與本目錄其他 crawler 一致：
            站方改版、暫時無回應等成因在此層分不出來，交由 updater 判斷。
        - Return:
            - Optional[str]
                big5 解碼後的 CSV 原文；取得失敗時為 None
        """

        return self.fetch_csv("TAIFEX_INDEX_MARGIN_URL", "指數類")

    def crawl_stock_margin(self) -> Optional[str]:
        """取得股票類的現行保證金一覽表（CSV 原文）；語意同 `crawl_index_margin()`"""

        return self.fetch_csv("TAIFEX_STOCK_MARGIN_URL", "股票類")

    # === 歷史調整公告 ===
    def crawl_announcements(
        self, start_date: datetime.date, end_date: datetime.date
    ) -> List[Dict[str, str]]:
        """
        - Description:
            查詢區間內的保證金調整公告清單

            **這是 POST 端點，且 `isQuery` 必須是 `"1"`**——傳 `"true"`／`"Y"`
            只會回一個沒有結果的空表單頁，**而且不報錯**。
            `newsType` 的值是中文字（`公告`）不是代碼。
        - Parameters:
            - start_date / end_date: datetime.date
                查詢區間
        - Return:
            - List[Dict[str, str]]
                `[{"date", "title", "link"}, ...]`，依日期排序；查無資料時為空 list
        """

        logger.info(
            f"* Start crawling TAIFEX margin announcements: {start_date} ~ {end_date}"
        )

        announcements: List[Dict[str, str]] = sorted(
            self.query_announcements(start_date, end_date, self.ANNOUNCEMENT_KEYWORD),
            key=lambda item: item["date"],
        )
        logger.info(
            f"* 關鍵字「{self.ANNOUNCEMENT_KEYWORD}」取得 {len(announcements)} 則"
            f"（不做標題篩選，由附件結構收斂）"
        )
        return announcements

    def query_announcements(
        self, start_date: datetime.date, end_date: datetime.date, keyword: str
    ) -> List[Dict[str, str]]:
        """以單一關鍵字查詢公告清單；查詢失敗時回傳空 list"""

        url: str = URLManager.get_url("TAIFEX_HISTORY_NEWS_URL")
        response: Optional[requests.Response] = RequestUtils.requests_post(
            url,
            data={
                "isQuery": self.ANNOUNCEMENT_QUERY_FLAG,
                "queryStartDate": start_date.strftime("%Y/%m/%d"),
                "queryEndDate": end_date.strftime("%Y/%m/%d"),
                "queryKeyWord": keyword,
                "newsType": self.ANNOUNCEMENT_NEWS_TYPE,
            },
        )

        if response is None:
            logger.warning(f"[Futures Margin] 取得公告清單失敗（關鍵字：{keyword}）")
            return []

        response.encoding = FileEncoding.UTF8.value
        announcements: List[Dict[str, str]] = []
        for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, re.S):
            date_match = re.search(r"(\d{4}/\d{2}/\d{2})", row_html)
            link_match = re.search(r'href="([^"]+)"', row_html)
            if date_match is None or link_match is None:
                continue
            announcements.append(
                {
                    "date": date_match.group(1),
                    "title": re.sub(r"<[^>]+>|\s+", " ", row_html).strip(),
                    "link": link_match.group(1),
                }
            )
        return announcements

    def resolve_announcement_csv(self, link: str) -> Optional[str]:
        """
        - Description:
            自公告連結解析出 CSV 附件的網址

            **附件檔名每則公告都不同**，組不出通則，只能從明細頁上抓。
            清單列有兩種連結：直接指向 `.pdf`（該則沒有明細頁，也沒有 CSV），
            或指向 `newsDetail` 頁（可能有 CSV）。
        - Parameters:
            - link: str
                公告清單列的連結（可能是相對路徑）
        - Return:
            - Optional[str]
                CSV 附件的絕對網址；該則沒有 CSV 時為 None
        """

        if link.lower().endswith(".pdf"):
            # 2015~2019 的公告只有掃描 PDF（無文字層），取不到數值
            return None

        url: str = urljoin(self.NEWS_DETAIL_BASE, link)
        response: Optional[requests.Response] = RequestUtils.requests_get(url)
        if response is None:
            logger.warning(f"[Futures Margin] 取得公告明細失敗：{url}")
            return None

        response.encoding = FileEncoding.UTF8.value
        attachments: List[str] = re.findall(r'href="([^"]*attach[^"]*)"', response.text)
        for attachment in attachments:
            if attachment.lower().endswith(".csv"):
                return attachment
        return None

    def crawl_announcement_csv(self, csv_url: str) -> Optional[str]:
        """取回公告附件的 CSV 原文（big5 解碼）"""

        response: Optional[requests.Response] = RequestUtils.requests_get(csv_url)
        if response is None:
            logger.warning(f"[Futures Margin] 下載公告附件失敗：{csv_url}")
            return None

        text: str = response.content.decode(FileEncoding.BIG5.value, errors="replace")
        return text if text.strip() else None

    @staticmethod
    def fetch_csv(url_name: str, label: str) -> Optional[str]:
        """
        - Description:
            取回 CSV 並以 big5 解碼

            **`errors="replace"` 是刻意的**：整份表因為一個罕見字就整批取不到，
            比留下一個問號字元糟得多；真正要用的欄位（金額、比例、代碼）都是
            ASCII，受影響的只有中文簡稱。
        - Parameters:
            - url_name: str
                `URLManager` 的 key
            - label: str
                log 用的類別名稱（指數類／股票類）
        - Return:
            - Optional[str]
                CSV 原文；取得失敗時為 None
        """

        logger.info(f"* Start crawling TAIFEX futures margin: {label}")

        url: str = URLManager.get_url(url_name)
        response: Optional[requests.Response] = RequestUtils.requests_get(url)

        if response is None:
            logger.warning(f"[Futures Margin] 取得{label}保證金一覽表失敗")
            return None

        text: str = response.content.decode(FileEncoding.BIG5.value, errors="replace")

        if not text.strip():
            logger.warning(f"[Futures Margin] {label}保證金一覽表為空")
            return None

        return text
