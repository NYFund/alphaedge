"""刪除 price 表中指定日期的資料

**這支腳本會不可逆地刪資料**，卻沒有任何預覽或確認：
打錯一個日期就少掉一整天、上千檔的收盤行情，而且要重跑 ETL 才補得回來。

故改為預設**只預覽不刪除**：

    python -m tasks.delete_price_data --date 2025-07-13              # 只報告
    python -m tasks.delete_price_data --date 2025-07-13 --apply      # 互動確認後刪除
    python -m tasks.delete_price_data --date 2025-07-13 --apply --yes  # 不確認（排程用）
"""

import argparse
import datetime
import sys
from typing import List

from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBError
from core.dao.tw.stock_price_dao import StockPriceDAO


def parse_date(date_str: str) -> str:
    """解析日期字串，回傳標準格式 YYYY-MM-DD"""
    # 嘗試多種日期格式
    formats: List[str] = [
        "%Y-%m-%d",  # 2025-07-13
        "%Y/%m/%d",  # 2025/7/13 或 2025/07/13
        "%Y-%m-%d",  # 2025-7-13
    ]

    for fmt in formats:
        try:
            date_obj: datetime.date = datetime.datetime.strptime(date_str, fmt).date()
            return date_obj.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"無法解析日期格式: {date_str}")


def confirm(formatted_date: str, count: int) -> bool:
    """互動確認；非互動環境（無 tty）一律視為未確認"""

    if not sys.stdin.isatty():
        logger.error("非互動環境無法確認，請改用 --yes")
        return False

    answer: str = input(
        f"確定要刪除 {formatted_date} 的 {count} 筆 price 資料嗎？"
        f"此操作不可逆。輸入日期以確認："
    ).strip()
    return answer == formatted_date


def delete_price_data_by_date(
    date_str: str,
    apply: bool = False,
    assume_yes: bool = False,
) -> None:
    """
    - Description:
        刪除 price 表中指定日期的所有資料

        **預設只預覽不刪除**：`apply` 為 True 才會真的寫入。
    - Parameters:
        - date_str: str
            要刪除的日期
        - apply: bool
            True 才實際刪除；False 只報告筆數
        - assume_yes: bool
            跳過互動確認（排程用）
    """

    # 解析日期
    try:
        formatted_date: str = parse_date(date_str)
    except ValueError as e:
        logger.error(f"日期解析失敗: {e}")
        return

    # 連接資料庫；路徑在呼叫當下從本模組讀取，測試才能以 monkeypatch 改寫
    dao: StockPriceDAO = StockPriceDAO(db_path=TW_STOCK_DB_PATH)

    try:
        # 先查詢要刪除的資料筆數
        count: int = dao.count_by_date(formatted_date)

        if count == 0:
            logger.warning(f"price table 中沒有日期為 {formatted_date} 的資料")
            return

        logger.info(f"{formatted_date} 在 price table 中有 {count} 筆資料")

        if not apply:
            logger.info("預覽模式：未刪除任何資料（加 --apply 才會實際執行）")
            return

        if not assume_yes and not confirm(formatted_date, count):
            logger.info("未確認，已取消")
            return

        # 刪除資料並提交
        dao.delete_by_date(formatted_date)
        dao.commit()

        # 驗證刪除結果
        remaining_count: int = dao.count_by_date(formatted_date)

        if remaining_count == 0:
            logger.info(f"✅ 成功刪除 {count} 筆資料")
        else:
            logger.warning(f"⚠️ 刪除後仍有 {remaining_count} 筆資料存在")

    except DBError as e:
        logger.error(f"資料庫操作失敗: {e}")
        dao.conn.rollback()
    finally:
        dao.close()


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="刪除 price table 中指定日期的資料"
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="要刪除的日期 (格式: YYYY-MM-DD 或 YYYY/MM/DD，例如: 2025-07-13 或 2025/7/13)",
    )

    parser.add_argument(
        "--apply",
        action="store_true",
        help="實際刪除；未指定時只預覽筆數（預設）",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="跳過互動確認（排程用；需搭配 --apply）",
    )

    args: argparse.Namespace = parser.parse_args()
    delete_price_data_by_date(args.date, apply=args.apply, assume_yes=args.yes)


if __name__ == "__main__":
    main()
