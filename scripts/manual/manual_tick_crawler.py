import datetime
from typing import List, Optional

import pandas as pd
import pytest
import shioaji as sj
from loguru import logger

from core.broker.tw.shioaji_session import ShioajiSession, login_read_only_sessions
from core.config import API_KEY, API_SECRET_KEY, TICK_DOWNLOADS_PATH
from core.pipeline.tw.cleaners.stock_tick_cleaner import StockTickCleaner
from core.pipeline.tw.crawlers.stock_tick_crawler import StockTickCrawler
from core.utils import TimeUtils

"""測試 StockTickCrawler：爬取與清洗，不寫入資料庫

**本檔是手動執行的腳本，不是可被 pytest 直接跑的測試。** 三個 `test_*` 函式都帶
必填參數（`stock_id`、`date`），pytest 會當成 fixture 去找而報
`fixture 'stock_id' not found`，且執行需要 Shioaji API 金鑰。
標記為 slow 讓 CI 的 `-m "not slow"` 略過；要真正改成自動化測試需另行改寫。
"""

pytestmark = pytest.mark.slow


def login() -> Optional[ShioajiSession]:
    """以 `.env` 的金鑰登入正式環境（不啟用憑證，只查資料）；失敗時回 None"""

    sessions: List[ShioajiSession] = login_read_only_sessions(
        [(API_KEY, API_SECRET_KEY)]
    )
    return sessions[0] if sessions else None


def test_crawler_only(stock_id: str, date: datetime.date):
    """只測試爬取功能，不保存檔案"""

    print(f"\n{'=' * 60}")
    print("測試爬取功能（不保存檔案）")
    print(f"{'=' * 60}")
    print(f"股票代號: {stock_id}")
    print(f"日期: {date}")

    crawler: StockTickCrawler = StockTickCrawler()
    session: Optional[ShioajiSession] = login()
    api_instance: Optional[sj.Shioaji] = session.api if session else None

    if api_instance is None:
        print("❌ API 登入失敗")
        return None

    print("✅ API 登入成功")

    print(f"\n開始爬取 {stock_id} 在 {date} 的 tick 資料...")
    df: Optional[pd.DataFrame] = crawler.crawl_stock_tick(api_instance, date, stock_id)

    if df is None or df.empty:
        print("❌ 沒有爬取到資料")
        session.close()
        return None

    print("✅ 爬取成功！")
    print(f"資料筆數: {len(df)}")
    print(f"資料欄位: {list(df.columns)}")
    print("\n前 5 筆資料:")
    print(df.head())

    session.close()

    return df


def test_crawler_and_cleaner(stock_id: str, date: datetime.date):
    """
    測試爬取和清洗功能，會保存 CSV 檔案到 TICK_DOWNLOADS_PATH

    - Parameters:
        - stock_id: str
            股票代號，例如 "2330"
        - date: datetime.date
            日期，例如 datetime.date(2024, 1, 15)
    """

    print(f"\n{'=' * 60}")
    print("測試爬取和清洗功能（會保存 CSV 檔案）")
    print(f"{'=' * 60}")
    print(f"股票代號: {stock_id}")
    print(f"日期: {date}")
    print(f"資料保存路徑: {TICK_DOWNLOADS_PATH}")

    crawler: StockTickCrawler = StockTickCrawler()
    cleaner: StockTickCleaner = StockTickCleaner()

    session: Optional[ShioajiSession] = login()
    api_instance: Optional[sj.Shioaji] = session.api if session else None

    if api_instance is None:
        print("❌ API 登入失敗")
        return None

    print("✅ API 登入成功")

    print(f"\n開始爬取 {stock_id} 在 {date} 的 tick 資料...")
    df: Optional[pd.DataFrame] = crawler.crawl_stock_tick(api_instance, date, stock_id)

    if df is None or df.empty:
        print("❌ 沒有爬取到資料")
        session.close()
        return None

    print(f"✅ 爬取成功！資料筆數: {len(df)}")

    # 清洗的同時就會把 CSV 寫到 TICK_DOWNLOADS_PATH
    print("\n開始清洗資料...")
    cleaned_df: Optional[pd.DataFrame] = cleaner.clean_stock_tick(df, stock_id)

    if cleaned_df is None or cleaned_df.empty:
        print("❌ 清洗後的資料為空")
        session.close()
        return None

    print("✅ 清洗成功！")
    print(f"清洗後資料筆數: {len(cleaned_df)}")
    print(f"清洗後資料欄位: {list(cleaned_df.columns)}")
    print("\n前 5 筆清洗後的資料:")
    print(cleaned_df.head())

    csv_file = TICK_DOWNLOADS_PATH / f"{stock_id}.csv"
    if csv_file.exists():
        file_size = csv_file.stat().st_size
        print(f"\n✅ CSV 檔案已保存: {csv_file}")
        print(f"檔案大小: {file_size:,} bytes ({file_size / 1024:.2f} KB)")
    else:
        print(f"\n⚠️  警告: CSV 檔案未找到於 {csv_file}")

    session.close()

    return cleaned_df


def test_multiple_dates(stock_id: str, dates: List[datetime.date]) -> None:
    """
    測試爬取多個日期的資料並合併

    - Parameters:
        - stock_id: str
            股票代號，例如 "2330"
        - dates: List[datetime.date]
            日期列表，例如 [datetime.date(2024, 1, 15), datetime.date(2024, 1, 16)]
    """

    print(f"\n{'=' * 60}")
    print("測試爬取多個日期的資料")
    print(f"{'=' * 60}")
    print(f"股票代號: {stock_id}")
    print(f"日期範圍: {dates[0]} ~ {dates[-1]} (共 {len(dates)} 天)")
    print(f"資料保存路徑: {TICK_DOWNLOADS_PATH}")

    crawler: StockTickCrawler = StockTickCrawler()
    cleaner: StockTickCleaner = StockTickCleaner()

    session: Optional[ShioajiSession] = login()
    api_instance: Optional[sj.Shioaji] = session.api if session else None

    if api_instance is None:
        print("❌ API 登入失敗")
        return None

    print("✅ API 登入成功")

    df_list: List[pd.DataFrame] = []
    for date in dates:
        print(f"\n爬取 {date} 的資料...")
        df: Optional[pd.DataFrame] = crawler.crawl_stock_tick(
            api_instance, date, stock_id
        )

        if df is not None and not df.empty:
            df_list.append(df)
            print(f"  ✅ 成功，取得 {len(df)} 筆資料")
        else:
            print("  ⚠️  沒有資料")

    if not df_list:
        print("\n❌ 所有日期都沒有爬取到資料")
        session.close()
        return None

    # 先合併再清洗：cleaner 一次寫一份 CSV，逐日清洗會互相覆蓋
    merged_df: pd.DataFrame = pd.concat(df_list, ignore_index=True)
    print(f"\n✅ 合併完成！總共 {len(merged_df)} 筆資料")

    print("\n開始清洗資料...")
    cleaned_df: Optional[pd.DataFrame] = cleaner.clean_stock_tick(merged_df, stock_id)

    if cleaned_df is None or cleaned_df.empty:
        print("❌ 清洗後的資料為空")
        session.close()
        return None

    print("✅ 清洗成功！")
    print(f"清洗後資料筆數: {len(cleaned_df)}")

    csv_file = TICK_DOWNLOADS_PATH / f"{stock_id}.csv"
    if csv_file.exists():
        file_size = csv_file.stat().st_size
        print(f"\n✅ CSV 檔案已保存: {csv_file}")
        print(f"檔案大小: {file_size:,} bytes ({file_size / 1024:.2f} KB)")
    else:
        print(f"\n⚠️  警告: CSV 檔案未找到於 {csv_file}")

    session.close()

    return cleaned_df


if __name__ == "__main__":
    # 只留單行訊息，讓 logger 輸出與 print 混在一起時仍然讀得下去
    logger.remove()
    logger.add(lambda msg: print(msg, end=""), format="{message}")

    print("\n" + "=" * 60)
    print("範例 1: 只測試爬取功能")
    print("=" * 60)
    # 要測別天或別檔就改這兩個值
    test_date = datetime.date(2024, 1, 15)
    test_stock = "2330"
    df1 = test_crawler_only(test_stock, test_date)

    print("\n" + "=" * 60)
    print("範例 2: 測試爬取和清洗功能（會保存 CSV）")
    print("=" * 60)
    df2 = test_crawler_and_cleaner(test_stock, test_date)

    print("\n" + "=" * 60)
    print("範例 3: 測試多個日期")
    print("=" * 60)
    dates = TimeUtils.generate_date_range(
        datetime.date(2024, 1, 15), datetime.date(2024, 1, 17)
    )
    df3 = test_multiple_dates(test_stock, dates)

    print("\n" + "=" * 60)
    print("測試完成！")
    print("=" * 60)
    print(f"\n📁 資料保存位置: {TICK_DOWNLOADS_PATH}")
    print("   如果執行了範例 2 或 3，CSV 檔案會保存在此目錄下")
    print("   檔案名稱格式: {stock_id}.csv (例如: 2330.csv)")
