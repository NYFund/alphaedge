# Python standard library
import argparse
import datetime
import sys
from pathlib import Path
from typing import List, Tuple

from loguru import logger

from core.config import (
    API_LOGS_DIR_PATH,
    BACKTEST_LOGS_DIR_PATH,
    LOGS_DIR_PATH,
    PIPELINE_LOGS_DIR_PATH,
)

"""
日誌清理進入點：刪除超過保留天數的舊 log

**為什麼需要這支，而不是調 `LogManager` 的 `retention`**：
loguru 的 retention 只在**該 logger 再次被建立時**才觸發清理。一支跑完就不再執行的
crawler（例如 `crawl_finmind`），它的舊檔會永遠留著——`logs/` 曾累積到 2.2 GB，
其中最舊的檔案比預設的 30 天保留期還久了半年。問題出在「不會被觸發」，
不是「保留太久」，所以解法是一個與 logger 生命週期無關的獨立進入端。

使用方式：
    python -m apps.clean_logs                    # 預覽（不刪）
    python -m apps.clean_logs --apply            # 實際刪除，預設保留 30 天
    python -m apps.clean_logs --apply --days 7   # 只保留 7 天
    python -m apps.clean_logs --apply --bucket api   # 只清 api 桶
"""

DEFAULT_RETENTION_DAYS: int = 30

# 三個桶的價值不同，故可個別指定：
# - api：每次查詢都寫，純雜訊，整桶刪掉也不影響任何事
# - pipeline：**會回頭讀**（回補的 N requested／N no data／N unreachable 統計行）
# - backtest：單次回測的執行紀錄
LOG_BUCKETS: dict = {
    "api": API_LOGS_DIR_PATH,
    "pipeline": PIPELINE_LOGS_DIR_PATH,
    "backtest": BACKTEST_LOGS_DIR_PATH,
}


def collect_stale_logs(log_dir: Path, days: int) -> List[Tuple[Path, int]]:
    """
    - Description:
        列出指定目錄下超過保留天數的 log 檔

        **只挑「已輪替」的檔案**（檔名帶時間戳，例如 `crawl_finmind.2026-01-28_*.log`）。
        當前使用中的 `crawl_finmind.log` 一律保留——刪掉正在被 loguru 寫入的檔案，
        該 handler 會繼續寫進一個已不存在的 inode，日誌就此靜默消失。
    - Parameters:
        - log_dir: Path
            要掃描的日誌目錄
        - days: int
            保留天數
    - Return:
        - List[Tuple[Path, int]]
            [(檔案路徑, 檔案大小 bytes)]
    """

    if not log_dir.exists():
        return []

    cutoff: float = (
        datetime.datetime.now() - datetime.timedelta(days=days)
    ).timestamp()

    stale: List[Tuple[Path, int]] = []
    for path in sorted(log_dir.glob("*.log")):
        # 輪替檔的檔名為 `<name>.<時間戳>.log`，比當前檔多一段；當前檔不動
        if path.name.count(".") < 2:
            continue

        if path.stat().st_mtime < cutoff:
            stale.append((path, path.stat().st_size))

    return stale


def clean_logs(days: int, buckets: List[str], apply: bool) -> None:
    """
    - Description:
        清理指定桶內的過期 log；預設只預覽不刪除
    - Parameters:
        - days: int
            保留天數
        - buckets: List[str]
            要清理的桶名
        - apply: bool
            True 才實際刪除
    """

    total_files: int = 0
    total_bytes: int = 0

    for bucket in buckets:
        log_dir: Path = LOG_BUCKETS[bucket]
        stale: List[Tuple[Path, int]] = collect_stale_logs(log_dir, days)

        if not stale:
            logger.info(f"[{bucket}] 無超過 {days} 天的輪替檔")
            continue

        bucket_bytes: int = sum(size for _, size in stale)
        logger.info(
            f"[{bucket}] {len(stale)} 個檔案、{bucket_bytes / 1024 / 1024:.1f} MB "
            f"超過 {days} 天"
        )

        for path, _ in stale:
            if apply:
                path.unlink()
            else:
                logger.debug(f"[{bucket}] 將刪除 {path.name}")

        total_files += len(stale)
        total_bytes += bucket_bytes

    action: str = "已刪除" if apply else "預覽（未刪除，加 --apply 才會實際執行）"
    logger.info(f"* {action}：{total_files} 個檔案、{total_bytes / 1024 / 1024:.1f} MB")


def parse_arguments() -> argparse.Namespace:
    """解析命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="清理過期的日誌檔（預設只預覽）"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"保留天數（預設 {DEFAULT_RETENTION_DAYS}）",
    )
    parser.add_argument(
        "--bucket",
        choices=list(LOG_BUCKETS.keys()),
        action="append",
        help="只清理指定的桶，可重複指定；未指定時三個桶全清",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="實際刪除；未指定時只列出會被刪的檔案",
    )
    return parser.parse_args()


def main() -> None:
    """進入點"""

    args: argparse.Namespace = parse_arguments()

    if not LOGS_DIR_PATH.exists():
        logger.warning(f"日誌目錄不存在：{LOGS_DIR_PATH}")
        sys.exit(0)

    buckets: List[str] = args.bucket or list(LOG_BUCKETS.keys())
    clean_logs(days=args.days, buckets=buckets, apply=args.apply)


if __name__ == "__main__":
    main()
