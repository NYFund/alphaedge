import argparse
import datetime
from pathlib import Path
from typing import Dict

import pandas as pd
from loguru import logger

from strategy_lab.data_analysis.tick_spread.aggregate import aggregate_range
from strategy_lab.data_analysis.tick_spread.analysis import run_analysis

"""
台股逐筆報價價差研究入口：替日 K 回測的逐標的滑價設定找依據

請於 AlphaEdge 專案根目錄以 `-m` 執行（需要執行中的 TimescaleDB 與研究庫 `tw_stock.db`）：
    .venv/bin/python -m strategy_lab.data_analysis.tick_spread.run
    .venv/bin/python -m strategy_lab.data_analysis.tick_spread.run --start 2023-05-01
    .venv/bin/python -m strategy_lab.data_analysis.tick_spread.run --skip-aggregate

彙總按月快取在 `output/daily_spread/`，中斷後重跑只補沒做完的月份；分析結果寫在 `output/`。
"""

TOPIC_DIR: Path = Path(__file__).resolve().parent
OUTPUT_DIR: Path = TOPIC_DIR / "output"
CACHE_DIR: Path = OUTPUT_DIR / "daily_spread"

# tick 資料的完整區間：歷史存檔 2020-04-01 起，止於 2024-05-10
DEFAULT_START: datetime.date = datetime.date(2020, 4, 1)
DEFAULT_END: datetime.date = datetime.date(2024, 5, 10)


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="台股逐筆報價價差研究"
    )
    parser.add_argument(
        "--start", type=datetime.date.fromisoformat, default=DEFAULT_START
    )
    parser.add_argument("--end", type=datetime.date.fromisoformat, default=DEFAULT_END)
    parser.add_argument(
        "--skip-aggregate", action="store_true", help="只用已快取的彙總跑分析"
    )
    parser.add_argument("--refresh", action="store_true", help="重算已快取的月份")
    return parser.parse_args()


def main() -> None:
    """彙總（可略過）後跑分析，結果印到 log 並寫成 CSV"""

    args: argparse.Namespace = parse_arguments()
    if not args.skip_aggregate:
        aggregate_range(args.start, args.end, CACHE_DIR, args.refresh)

    tables: Dict[str, pd.DataFrame] = run_analysis(CACHE_DIR, OUTPUT_DIR)
    for name, table in tables.items():
        logger.info(f"\n=== {name} ===\n{table.to_string()}")
    logger.info(f"輸出已寫入：{OUTPUT_DIR}")


if __name__ == "__main__":
    main()
