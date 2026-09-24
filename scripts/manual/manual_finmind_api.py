"""
測試 FinMindAPI 的每一個 function 是否正常運作

直接使用 tw_stock.db 唯讀查詢，不更動資料庫

使用方法（從專案根目錄執行）：
    python -m scripts.manual.manual_finmind_api

測試內容：`FinMindAPI` 的 11 個公開查詢方法（台股總覽、證券商資訊、券商分點），
每個都確認回傳型別正確並印出前幾筆；日期區間類另外驗 start > end 要回空表。

**需要 data/db/tw_stock.db 才能執行**，故標記為 slow：CI 環境沒有資料庫，
未標記會在 `pytest -m "not slow"` 直接以 sqlite3.OperationalError 失敗。
"""

import datetime
import sys

import pandas as pd
import pytest

from core.api.tw.finmind_api import FinMindAPI

pytestmark = pytest.mark.slow


def test_finmind_api() -> bool:
    """測試 FinMindAPI 所有方法（唯讀，使用 tw_stock.db）"""

    print("\n" + "=" * 60)
    print("測試 FinMindAPI 各 function（使用 tw_stock.db 唯讀）")
    print("=" * 60)

    api = FinMindAPI()
    all_ok = True

    def check(name: str, df: pd.DataFrame) -> None:
        nonlocal all_ok
        try:
            assert isinstance(df, pd.DataFrame), f"{name} 應回傳 DataFrame"
            print(f"[OK] {name}")
            if len(df) > 0:
                n = min(5, len(df))
                print(f"    前 {n} 筆（共 {len(df)} 筆）：")
                print(df.head(5).to_string(index=False))
            else:
                print("    筆數: 0（空 DataFrame）")
            print()
        except Exception as e:
            print(f"[X] {name}: {e}")
            all_ok = False

    df = api.get_stock_info("2330")
    check("get_stock_info(stock_id)", df)

    df = api.get_all_stock_info()
    check("get_all_stock_info()", df)

    df = api.get_stock_info_with_warrant("2330")
    check("get_stock_info_with_warrant(stock_id)", df)

    df = api.get_all_stock_info_with_warrant()
    check("get_all_stock_info_with_warrant()", df)

    df = api.get_broker_info("9A00")
    check("get_broker_info(securities_trader_id)", df)

    df = api.get_all_broker_info()
    check("get_all_broker_info()", df)

    d = datetime.date(2024, 7, 1)
    df = api.get_broker_trading_by_date(d)
    check("get_broker_trading_by_date(date)", df)

    start = datetime.date(2024, 7, 1)
    end = datetime.date(2024, 7, 15)
    df = api.get_broker_trading_range(start, end)
    check("get_broker_trading_range(start_date, end_date)", df)
    # start > end 一律回空表，不是錯誤
    df_empty = api.get_broker_trading_range(end, start)
    try:
        assert isinstance(df_empty, pd.DataFrame) and len(df_empty) == 0
        print("[OK] get_broker_trading_range(start > end) 回傳空 DataFrame")
        print("    筆數: 0（空 DataFrame）")
        print()
    except Exception as e:
        print(f"[X] get_broker_trading_range(start > end): {e}")
        all_ok = False

    df = api.get_broker_trading_for_stock_on_date("2330", d)
    check("get_broker_trading_for_stock_on_date(stock_id, date)", df)

    df = api.get_broker_trading_for_stock_in_range("2330", start, end)
    check("get_broker_trading_for_stock_in_range(stock_id, start_date, end_date)", df)
    df_empty = api.get_broker_trading_for_stock_in_range("2330", end, start)
    try:
        assert isinstance(df_empty, pd.DataFrame) and len(df_empty) == 0
        print(
            "[OK] get_broker_trading_for_stock_in_range(start > end) 回傳空 DataFrame"
        )
        print("    筆數: 0（空 DataFrame）")
        print()
    except Exception as e:
        print(f"[X] get_broker_trading_for_stock_in_range(start > end): {e}")
        all_ok = False

    # 券商中文名 + 日期；「合庫台中」是實際存在於 broker_info 的分點名
    df = api.get_broker_trading_by_broker_and_date("合庫台中", d)
    check("get_broker_trading_by_broker_and_date(securities_trader, date)", df)

    print("=" * 60)
    if all_ok:
        print("[完成] 所有 FinMindAPI 測試通過")
    else:
        print("[失敗] 部分測試未通過")
    print("=" * 60 + "\n")
    return all_ok


if __name__ == "__main__":
    ok = test_finmind_api()
    sys.exit(0 if ok else 1)
