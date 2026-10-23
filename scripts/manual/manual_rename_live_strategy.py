import argparse
import datetime
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from loguru import logger

from core.config.paths import LIVE_RESULT_DIR_PATH
from core.config.schema import TW_TRADING_DB_PATH
from core.dao.connection import connect_live_trading

"""
實盤紀錄庫的策略改名：把某支策略的歸屬鍵從舊類別名換成新類別名

**動機**：類別名同時是 `--strategy` 參數與紀錄庫的歸屬鍵（委託、成交、部位、交易模式、
parity 都以它分帳）。策略改名後不遷移紀錄庫的話，新名字的策略在帳上一筆部位都沒有，
舊名字的部位則變成沒人認領——下一次啟動對帳就會不一致。

改兩種地方，**在同一個交易內完成**，前後各表筆數必須相同：
- 每張表的 `strategy_name` 欄位（以 schema 動態找出，不寫死表名）。
- `live_run.strategy_params_json` 的 `strategies` 清單：parity 以它判斷「這支策略當天的
  交易段落有沒有正常跑完」，不改的話改名前的日子會被判成沒跑。
`live_run.end_reason` 之類的自由文字是歷史紀錄，**不改**。

另外把 `results/live/<舊名>/` 的 parity CSV 目錄改名（新名目錄已存在時不動，改為報告）。

**預設只列計畫**；`--apply` 才會先複製一份備份檔，再寫入。實盤行程是紀錄庫唯一的寫入者，
執行前要確認沒有 `apps.live` 行程在跑。

用法（專案根目錄）：
    uv run --no-sync python -m scripts.manual.manual_rename_live_strategy \\
        --old MomentumStrategy1 --new VolumeBreakoutMomentumStrategy
    uv run --no-sync python -m scripts.manual.manual_rename_live_strategy \\
        --old MomentumStrategy1 --new VolumeBreakoutMomentumStrategy --apply
"""

STRATEGY_COLUMN: str = "strategy_name"
RUN_TABLE: str = "live_run"
RUN_PARAMS_COLUMN: str = "strategy_params_json"


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="實盤紀錄庫的策略改名（預設只列計畫）"
    )
    parser.add_argument("--old", required=True, help="舊類別名")
    parser.add_argument("--new", required=True, help="新類別名")
    parser.add_argument(
        "--db", type=Path, default=TW_TRADING_DB_PATH, help="紀錄庫路徑"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=LIVE_RESULT_DIR_PATH,
        help="實盤結果目錄（parity CSV 依策略名分目錄）",
    )
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只列計畫）")
    return parser.parse_args()


def find_strategy_tables(conn: sqlite3.Connection) -> List[str]:
    """有 `strategy_name` 欄位的表；以 schema 動態找出，新增的表不會漏掉"""

    tables: List[str] = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return [
        table
        for table in tables
        if STRATEGY_COLUMN
        in {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    ]


def count_rows(conn: sqlite3.Connection) -> Dict[str, int]:
    """每張表的筆數；遷移前後必須相同"""

    tables: List[str] = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {
        table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        for table in tables
    }


def plan_strategy_columns(
    conn: sqlite3.Connection, old: str, new: str
) -> List[Tuple[str, int, int]]:
    """各表 `(表名, 舊名筆數, 新名筆數)`；新名已有資料代表兩邊會合併，要先人工確認"""

    return [
        (
            table,
            conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {STRATEGY_COLUMN} = ?', (old,)
            ).fetchone()[0],
            conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE {STRATEGY_COLUMN} = ?', (new,)
            ).fetchone()[0],
        )
        for table in find_strategy_tables(conn)
    ]


def rename_in_params(raw: str, old: str, new: str) -> str:
    """把 `strategy_params_json` 的 `strategies` 清單中的舊名換成新名；其餘欄位原樣保留"""

    params: Dict = json.loads(raw)
    strategies: List[str] = params.get("strategies", [])
    params["strategies"] = [new if name == old else name for name in strategies]
    return json.dumps(params, ensure_ascii=False)


def plan_run_params(conn: sqlite3.Connection, old: str) -> List[Tuple[str, str]]:
    """`live_run` 裡 `strategies` 清單含舊名的 `(run_id, 原 JSON)`"""

    rows: List[Tuple[str, str]] = []
    for run_id, raw in conn.execute(
        f"SELECT run_id, {RUN_PARAMS_COLUMN} FROM {RUN_TABLE} "
        f"WHERE {RUN_PARAMS_COLUMN} IS NOT NULL"
    ):
        if old in json.loads(raw).get("strategies", []):
            rows.append((run_id, raw))
    return rows


def apply_rename(conn: sqlite3.Connection, old: str, new: str) -> None:
    """在單一交易內改完所有表；任何一步失敗整批還原"""

    with conn:
        for table in find_strategy_tables(conn):
            conn.execute(
                f'UPDATE "{table}" SET {STRATEGY_COLUMN} = ? '
                f"WHERE {STRATEGY_COLUMN} = ?",
                (new, old),
            )
        for run_id, raw in plan_run_params(conn, old):
            conn.execute(
                f"UPDATE {RUN_TABLE} SET {RUN_PARAMS_COLUMN} = ? WHERE run_id = ?",
                (rename_in_params(raw, old, new), run_id),
            )


def main() -> None:
    """列計畫；帶 `--apply` 時備份後寫入並驗證"""

    args: argparse.Namespace = parse_arguments()
    if not args.db.exists():
        sys.exit(f"找不到紀錄庫：{args.db}")

    conn: sqlite3.Connection = connect_live_trading(args.db)
    columns: List[Tuple[str, int, int]] = plan_strategy_columns(
        conn, args.old, args.new
    )
    runs: List[Tuple[str, str]] = plan_run_params(conn, args.old)

    logger.info(f"{args.old} → {args.new}（{args.db}）")
    for table, old_count, new_count in columns:
        logger.info(
            f"  {table}.{STRATEGY_COLUMN}：{old_count} 筆舊名、{new_count} 筆新名"
        )
    logger.info(f"  {RUN_TABLE}.{RUN_PARAMS_COLUMN}：{len(runs)} 筆含舊名")

    merging: List[str] = [table for table, _, new_count in columns if new_count]
    if merging:
        sys.exit(
            f"新名已有資料（{merging}），改名會把兩支策略的帳合在一起；請先人工確認"
        )

    old_dir: Path = args.results_dir / args.old
    new_dir: Path = args.results_dir / args.new
    logger.info(f"  結果目錄：{old_dir} → {new_dir}（舊目錄存在：{old_dir.exists()}）")

    if not args.apply:
        logger.info("未帶 --apply，只列計畫，沒有寫入任何東西")
        return

    stamp: str = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup: Path = args.db.with_name(f"{args.db.stem}.before-rename-{stamp}.db")
    # 用 SQLite 的備份 API 而不是複製檔案：WAL 模式下還沒 checkpoint 的寫入在 -wal 檔裡
    with sqlite3.connect(backup) as target:
        conn.backup(target)
    logger.info(f"已備份：{backup}")

    before: Dict[str, int] = count_rows(conn)
    apply_rename(conn, args.old, args.new)
    after: Dict[str, int] = count_rows(conn)
    if before != after:
        sys.exit(f"筆數不同（前 {before}、後 {after}），請由備份 {backup} 還原")

    leftover: List[Tuple[str, int, int]] = [
        row for row in plan_strategy_columns(conn, args.old, args.new) if row[1]
    ]
    if leftover or plan_run_params(conn, args.old):
        sys.exit(f"仍有舊名殘留：{leftover}，請由備份 {backup} 還原")
    conn.close()

    if old_dir.exists() and not new_dir.exists():
        shutil.move(str(old_dir), str(new_dir))
        logger.info(f"結果目錄已改名：{new_dir}")
    elif old_dir.exists():
        logger.warning(f"{new_dir} 已存在，舊目錄 {old_dir} 未搬，請人工合併")

    logger.info("改名完成：各表筆數相同、沒有舊名殘留")


if __name__ == "__main__":
    main()
