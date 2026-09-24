import datetime
import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
from loguru import logger

from core.config import TICK_DOWNLOADS_PATH, TICK_METADATA_PATH
from core.pipeline.tw.crawlers.stock_info_crawler import StockInfoCrawler

"""
初始化 tick_metadata.json：爬取股票代號、建立預設 last_date、掃描 CSV 並更新 metadata

`last_date` 是 tick 更新的續跑依據，重建它的成本很低（重新掃一次 CSV），
但漏掉一檔就會讓那一檔從預設日整段重爬。
"""


def init_tick_metadata_with_default_date(default_date: str = "2024-5-10") -> None:
    """初始化 tick_metadata.json，使用預設日期（格式 YYYY-M-D 或 YYYY-MM-DD）"""

    print(f"\n{'=' * 60}")
    print("初始化 tick_metadata.json")
    print(f"{'=' * 60}")

    TICK_METADATA_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("\n步驟 1: 爬取所有上市櫃公司代號...")
    try:
        stock_list: List[str] = StockInfoCrawler.crawl_stock_list()
        print(f"✅ 成功爬取 {len(stock_list)} 檔股票")
    except Exception as e:
        logger.error(f"❌ 爬取股票列表失敗: {e}")
        raise

    try:
        default_date_obj: datetime.date = datetime.datetime.strptime(
            default_date, "%Y-%m-%d"
        ).date()
        default_date_str: str = default_date_obj.isoformat()  # "YYYY-MM-DD"
        print(f"\n步驟 2: 設定預設日期為 {default_date_str}")
    except ValueError:
        logger.error(
            f"❌ 日期格式錯誤: {default_date}，請使用 YYYY-M-D 或 YYYY-MM-DD 格式"
        )
        raise

    print("\n步驟 3: 創建預設的 metadata 結構...")
    metadata: Dict[str, Any] = {"stocks": {}}

    for stock_id in stock_list:
        metadata["stocks"][stock_id] = {"last_date": default_date_str}

    print(f"✅ 已為 {len(stock_list)} 檔股票設定預設日期 {default_date_str}")

    print("\n步驟 4: 掃描 CSV 檔案並更新 last_date...")

    if not TICK_DOWNLOADS_PATH.exists():
        logger.warning(f"⚠️  Tick 下載資料夾不存在: {TICK_DOWNLOADS_PATH}")
        logger.info("將使用預設日期建立 metadata")
    else:
        csv_files: List[Path] = list(TICK_DOWNLOADS_PATH.glob("*.csv"))
        print(f"找到 {len(csv_files)} 個 CSV 檔案")

        updated_count: int = 0
        for csv_file in csv_files:
            stock_id: str = csv_file.stem  # 檔名即股票代號

            # 不在股票清單中的多半是權證或已下市，不列入 metadata
            if stock_id not in metadata["stocks"]:
                logger.debug(f"跳過不在股票列表中的檔案: {csv_file.name}")
                continue

            try:
                # 只讀 time 欄：tick 檔動輒數十萬列，全欄讀進來沒有必要
                df: pd.DataFrame = pd.read_csv(csv_file, usecols=["time"])

                if df.empty:
                    logger.warning(f"檔案 {csv_file.name} 為空，跳過")
                    continue

                # CSV 依時間遞增寫入，最後一列即當檔最新的一筆
                last_time_str: str = df["time"].iloc[-1]

                try:
                    last_time: pd.Timestamp = pd.to_datetime(last_time_str)
                    last_date: datetime.date = last_time.date()
                    last_date_str: str = last_date.isoformat()

                    metadata["stocks"][stock_id]["last_date"] = last_date_str
                    updated_count += 1

                    logger.debug(
                        f"股票 {stock_id}: 從 CSV 更新 last_date = {last_date_str}"
                    )
                except Exception as e:
                    logger.warning(
                        f"無法解析檔案 {csv_file.name} 的時間 '{last_time_str}': {e}"
                    )
                    continue

            except Exception as e:
                logger.error(f"讀取檔案 {csv_file.name} 時發生錯誤: {e}")
                continue

        print(f"✅ 成功更新 {updated_count} 檔股票的 last_date（從 CSV 檔案）")

    print("\n步驟 5: 寫入 tick_metadata.json...")
    try:
        with open(TICK_METADATA_PATH, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=4)
        print(f"✅ 成功寫入 {TICK_METADATA_PATH}")
    except Exception as e:
        logger.error(f"❌ 寫入檔案失敗: {e}")
        raise

    print(f"\n{'=' * 60}")
    print("初始化完成！")
    print(f"{'=' * 60}")
    print(f"總股票數: {len(metadata['stocks'])}")
    print(
        f"使用預設日期 ({default_date_str}) 的股票數: {len(metadata['stocks']) - updated_count}"
    )
    print(f"從 CSV 更新日期的股票數: {updated_count}")
    print(f"Metadata 檔案位置: {TICK_METADATA_PATH}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    # 只留單行訊息，讓步驟輸出與 print 混在一起時仍然讀得下去
    logger.remove()
    logger.add(lambda msg: print(msg, end=""), format="{message}", level="INFO")

    default_date = "2024-5-10"
    init_tick_metadata_with_default_date(default_date=default_date)
