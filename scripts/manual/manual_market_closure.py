import argparse
import datetime
import sys
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

from core.api.tw.market_holiday_api import MarketHolidayAPI
from core.config import TW_STOCK_DB_PATH
from core.dao.tw.market_holiday_dao import MarketHolidayDAO

"""
臨時休市登記：颱風等不在交易所年初公告行事曆上的休市日

**動機**：交易日判定以官方開休市表為主來源，颱風休市不在表上，系統會把那天當交易日——
當天照跑段落，隔天的資料新鮮度檢查又把它當成「確定是交易日而資料缺漏」而中止。
直接補登在 `market_holiday` 沒用：`update-db` 每次整年替換那張表，下一次更新就洗掉。
本腳本寫進另一張 `market_adhoc_closure`，查詢休市日時兩張表合併。

**什麼時候登記**：臺北市宣布停止上班時，證券與期貨市場跟著停止交易。
- 前一晚公告後就登記：當天各段落以「非交易日」正常結束，不送單。
- 事後才登記也可以：休市後第一個開盤日的新鮮度檢查就不再中止。

**預設只列計畫**；`--apply` 才寫入。

用法（專案根目錄）：
    uv run --no-sync python -m scripts.manual.manual_market_closure --list
    uv run --no-sync python -m scripts.manual.manual_market_closure \\
        --date 2026-10-23 --reason "颱風停止交易" --apply
    uv run --no-sync python -m scripts.manual.manual_market_closure \\
        --remove 2026-10-23 --apply
"""


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="臨時休市登記（預設只列計畫）"
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--date", type=datetime.date.fromisoformat, help="要登記的休市日（YYYY-MM-DD）"
    )
    action.add_argument(
        "--remove",
        type=datetime.date.fromisoformat,
        help="撤銷登記的日期（登記錯時用）",
    )
    action.add_argument("--list", action="store_true", help="列出所有登記")
    parser.add_argument("--reason", help="休市原因；登記時必填")
    parser.add_argument("--db", type=Path, default=TW_STOCK_DB_PATH, help="研究庫路徑")
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只列計畫）")
    return parser.parse_args()


def add_closure(
    dao: MarketHolidayDAO, date: datetime.date, reason: str, apply: bool
) -> bool:
    """
    - Description:
        登記一天臨時休市

        週末與官方已列為休市的日子不必登記，直接略過：登記了也不改變判定，
        只會讓清單混進不是臨時休市的日子。
    - Parameters:
        - dao: MarketHolidayDAO
            研究庫的開休市日期 DAO
        - date: datetime.date
            休市日
        - reason: str
            原因
        - apply: bool
            實際寫入；False 只列計畫
    - Return:
        - bool
            有寫入（或 `apply=False` 時會寫入）為 True
    """

    if date.weekday() >= 5:
        logger.warning(f"{date} 是週末，本來就休市，不必登記")
        return False

    if date in dao.get_adhoc_closures(date, date):
        logger.info(f"{date} 已登記過臨時休市，不必再登記")
        return False

    if date in dao.get_closures(date, date):
        logger.info(f"{date} 是官方公告的休市日，不必登記")
        return False

    if not apply:
        logger.info(f"[計畫] 登記 {date} 臨時休市（{reason}）；加上 --apply 才寫入")
        return True

    dao.ensure_adhoc_table()
    inserted: bool = dao.add_adhoc_closure(date, reason)
    dao.commit()
    logger.info(f"已登記 {date} 臨時休市（{reason}）")
    return inserted


def remove_closure(dao: MarketHolidayDAO, date: datetime.date, apply: bool) -> bool:
    """
    - Description:
        撤銷一天臨時休市登記
    - Parameters:
        - dao: MarketHolidayDAO
            研究庫的開休市日期 DAO
        - date: datetime.date
            要撤銷的日期
        - apply: bool
            實際刪除；False 只列計畫
    - Return:
        - bool
            有刪除（或 `apply=False` 時會刪除）為 True
    """

    if date not in dao.get_adhoc_closures(date, date):
        logger.warning(f"{date} 沒有登記臨時休市，無可撤銷")
        return False

    if not apply:
        logger.info(f"[計畫] 撤銷 {date} 的臨時休市登記；加上 --apply 才刪除")
        return True

    removed: bool = dao.remove_adhoc_closure(date)
    dao.commit()
    logger.info(f"已撤銷 {date} 的臨時休市登記")
    return removed


def report_judgement(dao: MarketHolidayDAO, date: datetime.date) -> None:
    """印出該日在官方日曆來源的判定，讓登記的人當場確認結果"""

    answer: Optional[bool] = MarketHolidayAPI(conn=dao.conn).is_trading_day(date)
    label: str = {True: "交易日", False: "休市", None: "無法判定（年度未入庫）"}[answer]
    logger.info(f"{date} 目前判定：{label}")


def main() -> int:
    """依參數登記、撤銷或列出臨時休市"""

    args: argparse.Namespace = parse_arguments()
    dao: MarketHolidayDAO = MarketHolidayDAO(db_path=args.db)

    try:
        if args.list:
            closures: pd.DataFrame = dao.list_adhoc_closures()
            if closures.empty:
                logger.info("目前沒有登記任何臨時休市")
            for row in closures.itertuples(index=False):
                logger.info(f"{row.date}  {row.reason}（登記於 {row.created_at}）")
            return 0

        if args.date is not None:
            if not args.reason:
                logger.error("登記時要帶 --reason")
                return 2
            add_closure(dao, args.date, args.reason, args.apply)
            report_judgement(dao, args.date)
            return 0

        remove_closure(dao, args.remove, args.apply)
        report_judgement(dao, args.remove)
        return 0
    finally:
        dao.close()


if __name__ == "__main__":
    sys.exit(main())
