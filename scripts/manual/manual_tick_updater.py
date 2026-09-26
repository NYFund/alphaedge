import datetime
import sys
from pathlib import Path
from typing import List
from unittest.mock import MagicMock

import pytest
from loguru import logger

# **必須在 import StockTickUpdater 之前處理**：它在 import 時就會拉 dolphindb，
# 沒裝 `[tick]` 選用相依的機器會直接 ImportError，連爬取與清洗都試不了。
# 塞一個 mock 進 sys.modules 讓寫入路徑變成空操作，本腳本本來也不寫 DB
try:
    import dolphindb as ddb

    DOLPHINDB_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    mock_ddb = MagicMock()
    mock_session = MagicMock()
    mock_session.existsDatabase = MagicMock(return_value=False)
    mock_session.run = MagicMock()
    mock_session.close = MagicMock()
    mock_session.connect = MagicMock()
    mock_ddb.session = MagicMock(return_value=mock_session)
    sys.modules["dolphindb"] = mock_ddb
    DOLPHINDB_AVAILABLE = False
    print("⚠️  dolphindb 模組未安裝，使用 mock 模組（測試模式）")

from core.config import TICK_DOWNLOADS_PATH, TICK_METADATA_PATH
from core.pipeline.tw.updaters.stock_tick_updater import StockTickUpdater
from core.pipeline.tw.utils.stock_tick_utils import StockTickUtils

"""測試 StockTickUpdater.update：僅爬取與清洗，不寫入資料庫

**`test_update_without_db` 是手動執行的腳本，不是可被 pytest 直接跑的測試**
（帶必填參數 `start_date`，pytest 會當成 fixture 而報 not found）。
整檔標記為 slow，讓 CI 的 `-m "not slow"` 略過。
"""

pytestmark = pytest.mark.slow


def test_update_without_db(
    start_date: datetime.date, end_date: datetime.date = None
) -> None:
    """測試 StockTickUpdater.update，不存入資料庫"""

    if end_date is None:
        end_date = datetime.date.today()

    print(f"\n{'=' * 60}")
    print("測試 StockTickUpdater.update() - 不存入資料庫")
    print(f"{'=' * 60}")
    print(f"開始日期: {start_date}")
    print(f"結束日期: {end_date}")
    print(f"資料保存路徑: {TICK_DOWNLOADS_PATH}")

    print("\n初始化 StockTickUpdater...")
    updater: StockTickUpdater = StockTickUpdater()

    # 把入庫換成空操作：爬取與清洗照跑，但不寫進 DolphinDB
    def dummy_add_to_db(remove_file=False):
        """取代 `loader.add_to_db`，只記一行不做任何事"""

        logger.info("⚠️  跳過資料庫寫入（測試模式）")
        return None

    original_add_to_db = updater.loader.add_to_db
    updater.loader.add_to_db = dummy_add_to_db  # type: ignore

    print("✅ StockTickUpdater 初始化完成")
    print("✅ 已設定為測試模式（不會存入資料庫）")

    # 先記下既有檔數，跑完才分得出這一輪到底有沒有產出
    existing_files: List[Path] = list(TICK_DOWNLOADS_PATH.glob("*.csv"))
    print(f"\n📁 開始測試前，資料夾中現有 CSV 檔案數量: {len(existing_files)}")

    try:
        print("\n開始執行 update()...")
        print("這會執行：")
        print("  1. 爬取資料 (crawler.crawl_stock_tick)")
        print("  2. 清洗資料 (cleaner.clean_stock_tick)")
        print(f"  3. 保存 CSV 檔案到 {TICK_DOWNLOADS_PATH}")
        print("  4. ⚠️  跳過存入資料庫 (loader.add_to_db)")

        updater.update(start_date=start_date, end_date=end_date)

        print("\n✅ update() 執行完成！")

        new_files: List[Path] = list(TICK_DOWNLOADS_PATH.glob("*.csv"))
        print(f"\n📁 測試完成後，資料夾中 CSV 檔案數量: {len(new_files)}")
        print(f"📁 新增的 CSV 檔案數量: {len(new_files) - len(existing_files)}")

        if len(new_files) > len(existing_files):
            print("\n✅ 成功生成 CSV 檔案！")
            print("檔案列表（前 10 個）:")
            for i, csv_file in enumerate(new_files[:10], 1):
                file_size: int = csv_file.stat().st_size
                print(f"  {i}. {csv_file.name} ({file_size:,} bytes)")
            if len(new_files) > 10:
                print(f"  ... 還有 {len(new_files) - 10} 個檔案")
        else:
            print("⚠️  沒有新增 CSV 檔案（可能是日期範圍內沒有資料）")

        print(f"\n{'=' * 60}")
        print(f"📁 資料保存位置: {TICK_DOWNLOADS_PATH}")
        print("   所有爬取並清洗後的 CSV 檔案都保存在此目錄")
        print("   檔案名稱格式: {stock_id}.csv (例如: 2330.csv)")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n❌ 執行 update() 時發生錯誤: {e}")
        import traceback

        traceback.print_exc()
    finally:
        # 還原入庫方法，避免同一個 process 內後續誤用這個被掏空的 updater
        updater.loader.add_to_db = original_add_to_db


def test_scan_tick_downloads_folder():
    """測試 `StockTickUtils.scan_tick_downloads_folder()`：逐檔取出最後一筆資料的日期"""

    print(f"\n{'=' * 60}")
    print("測試 scan_tick_downloads_folder()")
    print(f"{'=' * 60}")
    print(f"資料夾路徑: {TICK_DOWNLOADS_PATH}")

    try:
        print("\n開始掃描 tick 下載資料夾...")
        stock_last_dates = StockTickUtils.scan_tick_downloads_folder()

        print("\n✅ 掃描完成！")
        print("📊 掃描結果統計：")
        print(f"   找到 {len(stock_last_dates)} 個股票的資料檔案")

        if stock_last_dates:
            print("\n📋 前 10 個股票的資訊：")
            for i, (stock_id, last_date) in enumerate(
                list(stock_last_dates.items())[:10], 1
            ):
                print(f"   {i}. {stock_id}: 最後一筆資料日期 = {last_date}")
            if len(stock_last_dates) > 10:
                print(f"   ... 還有 {len(stock_last_dates) - 10} 個股票")

            dates = list(stock_last_dates.values())
            unique_dates = sorted(set(dates))
            print("\n📅 日期範圍統計：")
            print(f"   最早日期: {min(unique_dates)}")
            print(f"   最晚日期: {max(unique_dates)}")
            print(f"   共有 {len(unique_dates)} 個不同的日期")
        else:
            print("\n⚠️  沒有找到任何 CSV 檔案")
            print(f"   請確認資料夾路徑是否正確：{TICK_DOWNLOADS_PATH}")
            print("   或者先執行 test_update_without_db() 來下載一些資料")

        return stock_last_dates

    except Exception as e:
        print(f"\n❌ 執行 scan_tick_downloads_folder() 時發生錯誤: {e}")
        import traceback

        traceback.print_exc()
        return {}


def test_update_tick_metadata_from_csv() -> None:
    """測試 `StockTickUtils.update_tick_metadata_from_csv()`：更新 `tick_metadata.json`"""

    print(f"\n{'=' * 60}")
    print("測試 update_tick_metadata_from_csv()")
    print(f"{'=' * 60}")

    metadata_path = TICK_METADATA_PATH
    print(f"Metadata 檔案路徑: {metadata_path}")

    try:
        # 先讀更新前的內容，才比得出這次增減了幾檔
        metadata_before = None
        if metadata_path.exists():
            with open(metadata_path, encoding="utf-8") as f:
                import json

                metadata_before = json.load(f)
                stocks_count_before = len(metadata_before.get("stocks", {}))
                print("\n📄 更新前的 metadata：")
                print(f"   已有 {stocks_count_before} 個股票的記錄")
        else:
            print("\n📄 metadata 檔案不存在，將創建新檔案")

        print("\n開始更新 tick_metadata...")
        StockTickUtils.update_tick_metadata_from_csv()

        print("\n✅ 更新完成！")

        if metadata_path.exists():
            with open(metadata_path, encoding="utf-8") as f:
                import json

                metadata_after = json.load(f)
                stocks_count_after = len(metadata_after.get("stocks", {}))

                print("\n📊 更新後的 metadata：")
                print(f"   共有 {stocks_count_after} 個股票的記錄")

                if metadata_before:
                    new_stocks = stocks_count_after - stocks_count_before
                    if new_stocks > 0:
                        print(f"   新增了 {new_stocks} 個股票的記錄")
                    elif new_stocks < 0:
                        print(f"   減少了 {abs(new_stocks)} 個股票的記錄")
                    else:
                        print("   股票數量沒有變化（可能已更新日期）")

                stocks = metadata_after.get("stocks", {})
                if stocks:
                    print("\n📋 前 5 個股票的資訊：")
                    for i, (stock_id, stock_info) in enumerate(
                        list(stocks.items())[:5], 1
                    ):
                        last_date = stock_info.get("last_date", "N/A")
                        print(f"   {i}. {stock_id}: last_date = {last_date}")

                print(f"\n📁 Metadata 檔案已保存至: {metadata_path}")
        else:
            print("\n⚠️  metadata 檔案未生成（可能沒有找到任何 CSV 檔案）")

    except Exception as e:
        print(f"\n❌ 執行 update_tick_metadata_from_csv() 時發生錯誤: {e}")
        import traceback

        traceback.print_exc()


def test_both_functions() -> None:
    """掃描與更新 metadata 一起跑，並驗證兩者看到的股票集合一致"""

    print(f"\n{'=' * 60}")
    print("測試 scan_tick_downloads_folder() 和 update_tick_metadata_from_csv()")
    print(f"{'=' * 60}")

    print("\n【步驟 1】測試 scan_tick_downloads_folder()")
    stock_last_dates = test_scan_tick_downloads_folder()

    print("\n【步驟 2】測試 update_tick_metadata_from_csv()")
    test_update_tick_metadata_from_csv()

    print("\n【步驟 3】驗證一致性")
    metadata_path = TICK_METADATA_PATH
    if metadata_path.exists():
        with open(metadata_path, encoding="utf-8") as f:
            import json

            metadata = json.load(f)
            metadata_stocks = set(metadata.get("stocks", {}).keys())
            scan_stocks = set(stock_last_dates.keys())

            if metadata_stocks == scan_stocks:
                print("✅ 驗證通過：metadata 中的股票與掃描結果一致")
                print(f"   兩者都包含 {len(metadata_stocks)} 個股票")
            else:
                print("⚠️  驗證發現差異：")
                only_in_metadata = metadata_stocks - scan_stocks
                only_in_scan = scan_stocks - metadata_stocks
                if only_in_metadata:
                    print(f"   只在 metadata 中: {only_in_metadata}")
                if only_in_scan:
                    print(f"   只在掃描結果中: {only_in_scan}")

    print(f"\n{'=' * 60}")
    print("✅ 所有測試完成！")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
        level="INFO",
    )

    import sys

    # 沒帶參數就走互動選單，帶了就直接跑那一項
    if len(sys.argv) == 1:
        print("\n" + "=" * 60)
        print("請選擇要執行的測試：")
        print("=" * 60)
        print("1. 測試 StockTickUpdater.update() - 不存入資料庫")
        print("2. 測試 scan_tick_downloads_folder() - 掃描下載資料夾")
        print("3. 測試 update_tick_metadata_from_csv() - 更新 metadata")
        print("4. 測試兩個函數的組合使用")
        print("5. 執行所有測試")
        print("=" * 60)
        choice = input("\n請輸入選項 (1-5): ").strip()
    else:
        choice = sys.argv[1]

    if choice == "1":
        # 要測別的區間就改這兩個值；end_date 省略時預設為今天
        test_start_date = datetime.date(2024, 5, 14)
        test_end_date = datetime.date(2024, 5, 15)

        print("\n" + "=" * 60)
        print("測試 StockTickUpdater.update() - 不存入資料庫")
        print("=" * 60)
        print("\n⚠️  注意：此測試會爬取所有上市櫃股票的資料")
        print("   如果日期範圍很大，可能會花費較長時間")
        print("   建議先用小範圍的日期測試（例如 1-2 天）")
        print("\n測試參數：")
        print(f"  開始日期: {test_start_date}")
        print(f"  結束日期: {test_end_date}")

        test_update_without_db(start_date=test_start_date, end_date=test_end_date)

        print("\n" + "=" * 60)
        print("測試完成！")
        print("=" * 60)

    elif choice == "2":
        test_scan_tick_downloads_folder()
        print("\n" + "=" * 60)
        print("測試完成！")
        print("=" * 60)

    elif choice == "3":
        test_update_tick_metadata_from_csv()
        print("\n" + "=" * 60)
        print("測試完成！")
        print("=" * 60)

    elif choice == "4":
        test_both_functions()

    elif choice == "5":
        print("\n" + "=" * 60)
        print("執行所有測試")
        print("=" * 60)

        print("\n【測試 1/3】scan_tick_downloads_folder()")
        test_scan_tick_downloads_folder()

        print("\n【測試 2/3】update_tick_metadata_from_csv()")
        test_update_tick_metadata_from_csv()

        print("\n【測試 3/3】組合測試")
        test_both_functions()

        print("\n" + "=" * 60)
        print("✅ 所有測試完成！")
        print("=" * 60)

    else:
        print(f"\n❌ 無效的選項: {choice}")
        print("請執行: python manual_tick_updater.py [1-5]")
