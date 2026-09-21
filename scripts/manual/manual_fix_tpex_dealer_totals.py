import argparse
import datetime
import sqlite3
from pathlib import Path
from typing import Tuple

import pandas as pd
from loguru import logger

from core.config import CHIP_TABLE_NAME, TW_STOCK_DB_PATH
from core.dao.connection import connect_sqlite
from core.pipeline.tw.cleaners.stock_chip_cleaner import StockChipCleaner

"""
一次性修正：上櫃中段（2014-12-01 ~ 2018-01-14）自營商買進／賣出合計欄

這段來源只有「自行買賣」「避險」兩組拆分欄，清洗端以前沒有把它們加成合計，
reindex 把合計欄補成 0，庫裡因此有十幾萬列「買賣皆 0、買賣超非 0」。
清洗端已修正，但入庫是 `INSERT OR IGNORE`，重爬蓋不掉舊列，故以本腳本就地更新。

只改「合計欄與拆分欄加總對不上」的列：同一段日期的上市資料本來就是對的，
條件式讓它們自然被排除。預設只統計不寫入；帶 `--apply` 才會：
1. 先把受影響列的 `(date, stock_id, 舊的買進合計, 舊的賣出合計)` 匯出成 CSV
   （整個 `tw_stock.db` 數 GB，整檔備份太重；還原只需要這兩欄的舊值）。
2. 在同一個交易內 UPDATE，完成後重新統計，異常就回滾。

用法（專案根目錄）：
    .venv/bin/python -m scripts.manual.manual_fix_tpex_dealer_totals
    .venv/bin/python -m scripts.manual.manual_fix_tpex_dealer_totals --apply
"""

START_DATE: str = StockChipCleaner.TPEX_FIRST_REFORM_DATE.isoformat()
END_DATE: str = StockChipCleaner.TPEX_SECOND_REFORM_DATE.isoformat()

BUY: str = '"自營商買進股數"'
SELL: str = '"自營商賣出股數"'
NET: str = '"自營商買賣超股數"'
SELF_BUY: str = '"自營商買進股數(自行買賣)"'
HEDGE_BUY: str = '"自營商買進股數(避險)"'
SELF_SELL: str = '"自營商賣出股數(自行買賣)"'
HEDGE_SELL: str = '"自營商賣出股數(避險)"'

# 中段、且合計欄與拆分欄加總對不上的列
MISMATCH_WHERE: str = (
    f"date >= ? AND date < ? "
    f"AND ({BUY} <> {SELF_BUY} + {HEDGE_BUY} OR {SELL} <> {SELF_SELL} + {HEDGE_SELL})"
)


def count_mismatch(conn: sqlite3.Connection) -> Tuple[int, int]:
    """回傳 `(要修的列數, 其中拆分欄算出的買賣超與庫內買賣超一致的列數)`"""

    row: Tuple[int, int] = conn.execute(
        f"""
        SELECT COUNT(*),
               COALESCE(SUM(
                   {NET} = ({SELF_BUY} + {HEDGE_BUY}) - ({SELF_SELL} + {HEDGE_SELL})
               ), 0)
        FROM {CHIP_TABLE_NAME}
        WHERE {MISMATCH_WHERE}
        """,
        (START_DATE, END_DATE),
    ).fetchone()
    return (int(row[0]), int(row[1]))


def count_impossible_rows(conn: sqlite3.Connection) -> int:
    """全表「買賣皆 0、買賣超非 0」的列數（修完後中段應歸零）"""

    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {CHIP_TABLE_NAME} "
            f"WHERE {BUY} = 0 AND {SELL} = 0 AND {NET} <> 0"
        ).fetchone()[0]
    )


def export_backup(conn: sqlite3.Connection, backup_path: Path) -> int:
    """把受影響列的舊值匯出成 CSV；回傳匯出列數"""

    backup_df: pd.DataFrame = pd.read_sql_query(
        f"SELECT date, stock_id, {BUY} AS buy_total, {SELL} AS sell_total "
        f"FROM {CHIP_TABLE_NAME} WHERE {MISMATCH_WHERE}",
        conn,
        params=(START_DATE, END_DATE),
    )
    backup_df.to_csv(backup_path, index=False)
    return len(backup_df)


def apply_fix(conn: sqlite3.Connection, backup_path: Path) -> None:
    """備份 → 更新 → 驗證，全部在同一個交易內；驗證失敗就回滾"""

    expected, consistent = count_mismatch(conn)
    if expected != consistent:
        raise RuntimeError(
            f"{expected} 列中只有 {consistent} 列的拆分欄與買賣超一致，"
            "拆分欄本身不可信，停止修正"
        )

    exported: int = export_backup(conn, backup_path)
    if exported != expected:
        raise RuntimeError(f"備份 {exported} 列與預期 {expected} 列不符，停止修正")
    logger.info(f"已備份 {exported} 列的舊值：{backup_path}")

    try:
        cursor: sqlite3.Cursor = conn.execute(
            f"UPDATE {CHIP_TABLE_NAME} "
            f"SET {BUY} = {SELF_BUY} + {HEDGE_BUY}, {SELL} = {SELF_SELL} + {HEDGE_SELL} "
            f"WHERE {MISMATCH_WHERE}",
            (START_DATE, END_DATE),
        )
        if cursor.rowcount != expected:
            raise RuntimeError(f"更新 {cursor.rowcount} 列與預期 {expected} 列不符")

        remaining, _ = count_mismatch(conn)
        if remaining != 0:
            raise RuntimeError(f"更新後仍有 {remaining} 列對不上")

        conn.commit()
    except Exception:
        conn.rollback()
        logger.error("修正失敗，已回滾；資料庫未變動")
        raise

    logger.info(f"已更新 {expected} 列")


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="修正上櫃中段自營商買進／賣出合計欄（預設只統計）"
    )
    parser.add_argument("--apply", action="store_true", help="實際寫入（會先備份）")
    args: argparse.Namespace = parser.parse_args()

    conn: sqlite3.Connection = connect_sqlite(
        TW_STOCK_DB_PATH, read_only=not args.apply
    )
    try:
        expected, consistent = count_mismatch(conn)
        logger.info(
            f"區間 {START_DATE} ~ {END_DATE}（不含）要修 {expected} 列，"
            f"其中 {consistent} 列拆分欄與買賣超一致"
        )
        logger.info(
            f"目前全表「買賣皆 0、買賣超非 0」：{count_impossible_rows(conn)} 列"
        )

        if not args.apply:
            logger.info("未帶 --apply，不寫入")
            return

        stamp: str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path: Path = (
            Path(TW_STOCK_DB_PATH).parent
            / f"chip_tpex_dealer_totals_backup_{stamp}.csv"
        )
        apply_fix(conn, backup_path)
        logger.info(
            f"修正後全表「買賣皆 0、買賣超非 0」：{count_impossible_rows(conn)} 列"
            "（中段以外的零星列是來源本身拆分欄也為 0，無從修正）"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
