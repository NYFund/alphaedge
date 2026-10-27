import datetime
from typing import Dict, List, Optional

from core.config import API_KEYS, API_SECRET_KEYS
from core.dao.tw.stock_tick_dao import StockTickDAO
from core.utils import ShioajiAPI

"""
台股 tick 更新的輔助工具：多組 Shioaji 金鑰與續跑判斷

**綁定台股，故與 `url_manager.py` 同樣歸在 `tw/` 底下**：報價來源是 Shioaji
（台灣券商），續跑紀錄的鍵是台股代號；每層目錄只承載一條軸，
跨市場共用的 `core/pipeline/utils/` 不放只有台股用得到的東西。

**續跑以 `stock_tick_load_log` 為準**：原本的 `tick_metadata.json` 是從 CSV 掃出來的，
入庫失敗時也會前進，下次就會跳過還沒進資料庫的日子。
"""


class StockTickUtils:
    """台股 tick 的金鑰與續跑判斷工具"""

    # 資料庫還沒有任何 tick 時的預設起點（Shioaji tick 只回溯到 2020-03-02）
    TICK_DEFAULT_FALLBACK_DATE: datetime.date = datetime.date(2020, 4, 1)

    @staticmethod
    def get_table_latest_date(dao: StockTickDAO) -> datetime.date:
        """
        - Description:
            資料庫裡 tick 的最新交易日；還沒有任何紀錄時回傳預設起點
        - Parameters:
            - dao: StockTickDAO
                tick 的 DAO
        - Return:
            - datetime.date
                各股票之中最新的那一天
        """

        latest: Optional[datetime.date] = dao.get_latest_trade_date()
        return latest if latest else StockTickUtils.TICK_DEFAULT_FALLBACK_DATE

    @staticmethod
    def get_loaded_last_dates(dao: StockTickDAO) -> Dict[str, datetime.date]:
        """
        - Description:
            每檔股票已入庫的最後交易日

            **整次更新只查一次**，再傳給各個 thread：逐檔查的話一次更新要打上千次資料庫，
            而這份結果在爬取期間不會變（入庫在爬完之後才做）。
        - Parameters:
            - dao: StockTickDAO
                tick 的 DAO
        - Return:
            - Dict[str, datetime.date]
                股票代號 → 最後交易日
        """

        return dao.get_loaded_last_dates()

    @staticmethod
    def setup_shioaji_apis() -> List[ShioajiAPI]:
        """
        - Description:
            依設定檔中的金鑰組建立所有 Shioaji API 連線

            逐筆行情有每把金鑰的請求上限，故備多組金鑰輪流下載；
            金鑰與密鑰以 `zip` 配對，數量不一致時以短的那一邊為準。
        - Return:
            - List[ShioajiAPI]
                依設定順序建立的 API 清單
        """

        api_list: List[ShioajiAPI] = []
        for key, secret in zip(API_KEYS, API_SECRET_KEYS):
            api: ShioajiAPI = ShioajiAPI(key, secret)
            api_list.append(api)
        return api_list

    @staticmethod
    def check_date_crawled(
        loaded_last_dates: Dict[str, datetime.date],
        stock_id: str,
        date: datetime.date,
    ) -> bool:
        """
        - Description:
            某檔股票的某個日期是否已經入庫

            **只要不晚於該股票已入庫的最後一天就視為已爬**：tick 是逐日往後補的，
            停牌日也不會因此每次重爬。前提是寫入時不留洞——updater 遇到某天爬取失敗，
            只寫入失敗日之前的日子，下一次就會從失敗日接著爬。
        - Parameters:
            - loaded_last_dates: Dict[str, datetime.date]
                `get_loaded_last_dates()` 的結果
            - stock_id: str
                股票代號
            - date: datetime.date
                要檢查的日期
        - Return:
            - bool
                True 表示已入庫、不必再爬
        """

        last_date: Optional[datetime.date] = loaded_last_dates.get(stock_id)
        return last_date is not None and date <= last_date
