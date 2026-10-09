import argparse
import datetime
import json
import random
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.api.tw.stock_tick_api import StockTickAPI
from core.config.paths import DATA_DIR_PATH
from core.config.schema import TW_STOCK_DB_PATH
from core.dao.timescale import tick_db_error_types
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.dao.tw.stock_tick_dao import StockTickDAO
from core.pipeline.tw.loaders.stock_tick_loader import (
    HISTORY_TIME_COLUMN,
    TICK_COLUMNS,
    StockTickLoader,
    add_trade_date_and_seq,
    filter_tick_rows,
    normalize_tick_frame,
)
from core.utils.log_manager import LogManager

"""
台股 tick 歷史 CSV 全量匯入 TimescaleDB，並做完整性比對

**預設只列計畫**（來源檔數、大小、推估耗時、資料庫現況），`--apply` 才會寫入。
來源是「一檔股票一個檔、涵蓋四年」的歷史 CSV；寫入與排除規則全部沿用 `StockTickLoader`，
日常更新與歷史匯入因此是同一套規則、同一個對帳口徑。

流程：
1. **依 chunk 切檔**：一次掃完全部來源，把每檔股票依 chunk 範圍切成
   `<工作目錄>/<chunk 起日>/<代號>.csv`（原格式不動），同時記下每個「股票 × 交易日」的來源列數。
   **不逐週重讀整個來源檔**：最大的 `2303.csv` 有 1 GB，每週重讀一次是兩百多倍的 I/O。
2. **逐 chunk 載入 → 壓縮**：峰值只多一個 chunk 的未壓縮資料（約 1 GB）。
   範圍對齊 TimescaleDB 的 chunk 邊界（從 1970-01-01（週四）起算的 7 天），不是日曆週：
   `compress_chunks_before()` 只壓整段都早於該日的 chunk，對不齊的話每一輪都會留一個沒壓的。
   某個 chunk 全部載入成功才刪它的切檔，失敗的留著給 `--resume` 重跑。
3. **期間停用壓縮 policy**、結束（含中斷）時恢復：policy 以「現在」往回算，
   2020～2024 的 chunk 全部符合條件，背景 job 會把還在載入的 chunk 壓掉。
4. **完整性比對**：每個「股票 × 交易日」的 CSV 列數＝`load_log.source_rows`、
   DB 列數＝`load_log.row_count`；抽樣逐列比對；抽樣比對每日成交量與 `price` 表；
   檢查 14 天以前的 chunk 是否都已壓縮。

`--resume`：已完整入庫（`load_log` 的來源列數與切檔一致）的「股票 × 交易日」跳過；
切檔已完成時沿用工作目錄，不重切。中斷後以同樣參數加 `--resume` 重跑即可。

用法（專案根目錄；先開 Docker Desktop 並 `docker compose up -d postgres`）：
    TICK_DATABASE_URL=... uv run --no-sync python -m scripts.manual.manual_tick_history_import
    TICK_DATABASE_URL=... uv run --no-sync python -m scripts.manual.manual_tick_history_import --apply
    TICK_DATABASE_URL=... uv run --no-sync python -m scripts.manual.manual_tick_history_import --apply --resume
    TICK_DATABASE_URL=... uv run --no-sync python -m scripts.manual.manual_tick_history_import --verify-only
"""

DEFAULT_SOURCE_DIR: Path = DATA_DIR_PATH / "tick_history"
DEFAULT_WORK_DIR: Path = DATA_DIR_PATH / "tick_import_work"

# chunk 的對齊起點：TimescaleDB 以 1970-01-01 為原點切固定長度的 chunk
CHUNK_ORIGIN: datetime.date = datetime.date(1970, 1, 1)

# 切檔完成的標記與「股票 × 交易日」來源列數；兩者都在工作目錄，`--resume` 據此沿用切檔
SPLIT_MARKER: str = "split_done.json"
SOURCE_COUNTS_FILE: str = "source_counts.csv"

# 切檔時每次讀入的列數：一次讀 1 GB 的檔要數 GB 記憶體，分批讀就不受檔案大小影響
READ_CHUNK_ROWS: int = 2_000_000

# 抽樣試點（19 檔 × 3 天寫入 TimescaleDB）實測的整體寫入速度（含讀檔與正規化）與 CSV 每列大小，只用來推估耗時
PILOT_ROWS_PER_SEC: float = 88_000
CSV_BYTES_PER_ROW: float = 57

# 抽樣比對的數量
SAMPLE_STOCK_DAYS: int = 20
SAMPLE_VOLUME_DAYS: int = 5


def parse_arguments() -> argparse.Namespace:
    """命令列參數"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="台股 tick 歷史 CSV 全量匯入 TimescaleDB（預設只列計畫）"
    )
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_WORK_DIR,
        help="依 chunk 切檔的暫存目錄；峰值約等於來源大小，載入成功的 chunk 會逐一刪除",
    )
    parser.add_argument("--start-date", type=datetime.date.fromisoformat, default=None)
    parser.add_argument("--end-date", type=datetime.date.fromisoformat, default=None)
    parser.add_argument("--schema", default="public", help="寫入的 schema（測試用）")
    parser.add_argument("--price-db", type=Path, default=TW_STOCK_DB_PATH)
    parser.add_argument("--apply", action="store_true", help="實際寫入（預設只列計畫）")
    parser.add_argument(
        "--resume", action="store_true", help="跳過已完整入庫的股票 × 交易日"
    )
    parser.add_argument(
        "--verify-only", action="store_true", help="只跑完整性比對，不匯入"
    )
    parser.add_argument("--seed", type=int, default=0, help="抽樣比對的亂數種子")
    return parser.parse_args()


# === chunk 邊界 ===
def chunk_start(day: datetime.date) -> datetime.date:
    """`day` 所在 chunk 的起日（含）"""

    interval: int = StockTickDAO.CHUNK_INTERVAL.days
    return day - datetime.timedelta(days=(day - CHUNK_ORIGIN).days % interval)


def chunk_end(start: datetime.date) -> datetime.date:
    """chunk 的終日（不含）"""

    return start + StockTickDAO.CHUNK_INTERVAL


# === 切檔 ===
def detect_time_column(csv_path: Path) -> str:
    """CSV 的時間欄名：歷史格式是 `ts`、cleaner 格式是 `time`"""

    header: List[str] = pd.read_csv(csv_path, nrows=0).columns.tolist()
    for column in (HISTORY_TIME_COLUMN, "time"):
        if column in header:
            return column
    raise ValueError(f"{csv_path.name} 沒有時間欄（{header}）")


def split_by_chunk(
    source_dir: Path,
    work_dir: Path,
    start_date: Optional[datetime.date],
    end_date: Optional[datetime.date],
) -> pd.DataFrame:
    """
    - Description:
        把來源 CSV 依 chunk 切到工作目錄，回傳每個「股票 × 交易日」的來源列數

        列序與欄序原樣保留：`seq` 是同一天內的原始列序，同一天的列一定落在同一個切檔、
        順序不變，切檔後算出來的 `seq` 和整檔算的一樣。完成時寫入標記檔，中途中斷不寫，
        下次會整個重切，不會沿用半套的切檔。
    - Parameters:
        - source_dir: Path
            歷史 CSV 目錄
        - work_dir: Path
            切檔目錄；開始前清空
        - start_date: Optional[datetime.date]
            只取這一天（含）之後
        - end_date: Optional[datetime.date]
            只取這一天（含）之前
    - Return:
        - pd.DataFrame
            `stock_id, trade_date, rows`
    """

    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True)

    csv_files: List[Path] = sorted(source_dir.glob("*.csv"))
    counts: List[pd.DataFrame] = []
    started: float = time.time()
    for index, csv_path in enumerate(csv_files, start=1):
        stock_id: str = csv_path.stem
        time_column: str = detect_time_column(csv_path)
        written_headers: Set[str] = set()
        for part in pd.read_csv(
            csv_path, dtype=str, keep_default_na=False, chunksize=READ_CHUNK_ROWS
        ):
            days: pd.Series = part[time_column].str.slice(0, 10)
            mask: pd.Series = pd.Series(True, index=part.index)
            if start_date is not None:
                mask &= days >= start_date.isoformat()
            if end_date is not None:
                mask &= days <= end_date.isoformat()
            part, days = part[mask], days[mask]
            if part.empty:
                continue

            chunk_of_day: Dict[str, str] = {
                day: chunk_start(datetime.date.fromisoformat(day)).isoformat()
                for day in days.unique()
            }
            chunk_keys: pd.Series = days.map(chunk_of_day)
            for key, rows in part.groupby(chunk_keys, sort=False):
                target_dir: Path = work_dir / key
                target_dir.mkdir(exist_ok=True)
                rows.to_csv(
                    target_dir / csv_path.name,
                    mode="a",
                    header=key not in written_headers,
                    index=False,
                )
                written_headers.add(key)
            day_counts: pd.Series = days.value_counts()
            counts.append(
                pd.DataFrame(
                    {
                        "stock_id": stock_id,
                        "trade_date": day_counts.index,
                        "rows": day_counts.to_numpy(),
                    }
                )
            )
        if index % 100 == 0 or index == len(csv_files):
            logger.info(
                f"[切檔] {index}/{len(csv_files)} 檔，{time.time() - started:.0f} 秒"
            )

    source_counts: pd.DataFrame = (
        pd.concat(counts, ignore_index=True)
        .groupby(["stock_id", "trade_date"], as_index=False)["rows"]
        .sum()
        if counts
        else pd.DataFrame(columns=["stock_id", "trade_date", "rows"])
    )
    source_counts.to_csv(work_dir / SOURCE_COUNTS_FILE, index=False)
    (work_dir / SPLIT_MARKER).write_text(
        json.dumps(
            {
                "source_dir": str(source_dir),
                "source_files": len(csv_files),
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
                "rows": int(source_counts["rows"].sum()),
            }
        ),
        encoding="utf-8",
    )
    return source_counts


def load_split_if_reusable(
    work_dir: Path,
    source_dir: Path,
    start_date: Optional[datetime.date],
    end_date: Optional[datetime.date],
) -> Optional[pd.DataFrame]:
    """切檔已完成、且參數相同時回傳來源列數；否則 None（要重切）"""

    marker_path: Path = work_dir / SPLIT_MARKER
    if not marker_path.exists():
        return None
    marker: Dict[str, Any] = json.loads(marker_path.read_text(encoding="utf-8"))
    expected: Dict[str, Optional[str]] = {
        "source_dir": str(source_dir),
        "start_date": start_date.isoformat() if start_date else None,
        "end_date": end_date.isoformat() if end_date else None,
    }
    if any(marker.get(key) != value for key, value in expected.items()):
        logger.warning(f"切檔參數不同（{marker}），重新切檔")
        return None
    return pd.read_csv(work_dir / SOURCE_COUNTS_FILE, dtype={"stock_id": str})


# === 載入 ===
def import_chunks(
    work_dir: Path,
    dao: StockTickDAO,
    loader: StockTickLoader,
    resume: bool,
) -> List[str]:
    """
    - Description:
        依 chunk 起日順序逐一載入、壓縮；成功的 chunk 刪掉切檔
    - Return:
        - List[str]
            失敗的切檔（`<chunk 起日>/<代號>.csv`）
    """

    chunk_dirs: List[Path] = sorted(p for p in work_dir.iterdir() if p.is_dir())
    failed: List[str] = []
    started: float = time.time()
    for index, chunk_dir in enumerate(chunk_dirs, start=1):
        start: datetime.date = datetime.date.fromisoformat(chunk_dir.name)
        end: datetime.date = chunk_end(start)
        files: List[Path] = sorted(chunk_dir.glob("*.csv"))
        pending: List[Path] = (
            [f for f in files if not loader.is_fully_loaded(f)] if resume else files
        )

        chunk_failed: List[str] = []
        written: int = 0
        if pending:
            # 重灌已壓縮的 chunk 先解壓：直接寫壓縮 chunk 每一列都要走壓縮資料的 DML
            dao.decompress_chunks_between(
                datetime.datetime.combine(start, datetime.time()),
                datetime.datetime.combine(end, datetime.time()),
            )
            for csv_path in pending:
                # 逐檔隔離：一檔失敗不擋同一 chunk 的其他檔；只收格式、讀檔與資料庫錯誤
                try:
                    written += loader.load_csv(csv_path)
                except (ValueError, OSError, *tick_db_error_types()) as error:
                    logger.error(
                        f"[匯入] {chunk_dir.name}/{csv_path.name} 失敗：{error}"
                    )
                    chunk_failed.append(f"{chunk_dir.name}/{csv_path.name}")
            check_chunk_boundary(dao, start, end)

        dao.compress_chunks_before(end)
        if chunk_failed:
            failed.extend(chunk_failed)
        else:
            shutil.rmtree(chunk_dir)

        elapsed: float = time.time() - started
        remaining: float = elapsed / index * (len(chunk_dirs) - index)
        logger.info(
            f"[匯入] chunk {start}～{end}（{index}/{len(chunk_dirs)}）："
            f"{len(pending)}/{len(files)} 檔、寫入 {written:,} 列、失敗 {len(chunk_failed)} 檔；"
            f"已 {elapsed / 60:.1f} 分、預估剩 {remaining / 60:.1f} 分"
        )
    return failed


def check_chunk_boundary(
    dao: StockTickDAO, start: datetime.date, end: datetime.date
) -> None:
    """
    確認資料庫實際的 chunk 和切檔的範圍一致

    不一致代表 chunk 原點或間隔和這支腳本的假設不同：繼續跑下去每一輪都會留一個沒壓的 chunk，
    下一輪又往壓縮過的 chunk 寫，所以當場中止。
    """

    expected: Tuple[datetime.datetime, datetime.datetime] = (
        datetime.datetime.combine(start, datetime.time()),
        datetime.datetime.combine(end, datetime.time()),
    )
    ranges: List[Tuple[datetime.datetime, datetime.datetime]] = [
        (range_start, range_end) for range_start, range_end, _ in dao.get_chunk_ranges()
    ]
    overlapping: List[Tuple[datetime.datetime, datetime.datetime]] = [
        r for r in ranges if r[0] < expected[1] and r[1] > expected[0]
    ]
    if overlapping and overlapping != [expected]:
        raise RuntimeError(
            f"chunk 邊界和切檔不一致：切檔 {expected}，資料庫 {overlapping}"
        )


# === 比對 ===
def verify(
    dao: StockTickDAO,
    price_dao: StockPriceDAO,
    source_counts: pd.DataFrame,
    source_dir: Path,
    seed: int,
) -> bool:
    """
    - Description:
        完整性比對；每一項都印出結果，回傳是否全數通過
    - Return:
        - bool
            列數比對與壓縮檢查都通過時為 True（成交量對照只報告、不判定）
    """

    passed: bool = True
    table: str = f'"{dao.schema}"."{dao.TABLE_NAME}"'
    load_log_table: str = f'"{dao.schema}"."{dao.LOAD_LOG_TABLE_NAME}"'

    def query(sql: str) -> List[Tuple[Any, ...]]:
        with dao.conn.transaction():
            return dao.conn.execute(sql).fetchall()

    # 1. 來源列數 ＝ load_log.source_rows（每個股票 × 交易日）
    log: pd.DataFrame = pd.DataFrame(
        query(
            f"SELECT stock_id, trade_date, source_rows, row_count FROM {load_log_table}"
        ),
        columns=["stock_id", "trade_date", "source_rows", "row_count"],
    )
    log["trade_date"] = log["trade_date"].astype(str)
    merged: pd.DataFrame = source_counts.assign(
        trade_date=source_counts["trade_date"].astype(str)
    ).merge(log, on=["stock_id", "trade_date"], how="outer", indicator=True)
    missing_log: pd.DataFrame = merged[merged["_merge"] == "left_only"]
    extra_log: pd.DataFrame = merged[merged["_merge"] == "right_only"]
    source_mismatch: pd.DataFrame = merged[
        (merged["_merge"] == "both") & (merged["rows"] != merged["source_rows"])
    ]
    logger.info(
        f"[比對 1] 股票 × 交易日：來源 {len(source_counts):,} 組、load_log {len(log):,} 組；"
        f"缺登記 {len(missing_log)}、多登記 {len(extra_log)}、來源列數不符 {len(source_mismatch)}"
    )
    if len(missing_log) or len(extra_log) or len(source_mismatch):
        passed = False
        for name, frame in (
            ("缺登記", missing_log),
            ("多登記", extra_log),
            ("來源列數不符", source_mismatch),
        ):
            if len(frame):
                logger.error(
                    f"  {name}（前 10 筆）：{frame.head(10).to_dict('records')}"
                )

    # 2. DB 列數 ＝ load_log.row_count（每個股票 × 交易日）
    db_mismatch: List[Tuple[Any, ...]] = query(
        f"""
        SELECT coalesce(l.stock_id, t.stock_id), coalesce(l.trade_date, t.trade_date),
               l.row_count, coalesce(t.n, 0)
        FROM {load_log_table} l
        FULL JOIN (
            SELECT stock_id, time::date AS trade_date, count(*) AS n FROM {table} GROUP BY 1, 2
        ) t ON t.stock_id = l.stock_id AND t.trade_date = l.trade_date
        WHERE coalesce(t.n, 0) <> coalesce(l.row_count, -1)
        LIMIT 20
        """
    )
    totals: Tuple[Any, ...] = query(
        f"SELECT count(*), coalesce(sum(source_rows), 0), coalesce(sum(row_count), 0) "
        f"FROM {load_log_table}"
    )[0]
    db_rows: int = query(f"SELECT count(*) FROM {table}")[0][0]
    logger.info(
        f"[比對 2] DB {db_rows:,} 列；load_log 來源 {totals[1]:,} 列、寫入 {totals[2]:,} 列"
        f"（排除 {totals[1] - totals[2]:,} 列）；DB 與 row_count 不符 {len(db_mismatch)} 組"
    )
    if db_mismatch or db_rows != totals[2]:
        passed = False
        logger.error(f"  不符（前 20 筆）：{db_mismatch}")

    # 3. 抽樣逐列比對：DB 讀回來的資料 ＝ 來源 CSV 正規化、排除後的資料
    passed &= verify_sample_rows(dao, price_dao, log, source_dir, seed)

    # 4. 抽樣對照每日成交量與 price 表（盤後零股與定價交易會有小幅落差，只報告）
    report_daily_volume(dao, price_dao, log, seed)

    # 5. 14 天以前的 chunk 都已壓縮
    cutoff: datetime.datetime = datetime.datetime.now() - StockTickDAO.COMPRESS_AFTER
    uncompressed: List[Tuple[datetime.datetime, datetime.datetime, bool]] = [
        r for r in dao.get_chunk_ranges() if r[1] <= cutoff and not r[2]
    ]
    logger.info(
        f"[比對 5] chunk 共 {len(dao.get_chunk_ranges())} 個；"
        f"14 天以前仍未壓縮 {len(uncompressed)} 個"
    )
    if uncompressed:
        passed = False
        logger.error(f"  未壓縮（前 10 個）：{uncompressed[:10]}")

    logger.info(f"[比對] {'全數通過' if passed else '有不符項目，見上方 ERROR'}")
    return passed


def verify_sample_rows(
    dao: StockTickDAO,
    price_dao: StockPriceDAO,
    log: pd.DataFrame,
    source_dir: Path,
    seed: int,
) -> bool:
    """抽樣「股票 × 交易日」逐列比對 DB 與來源 CSV（同一套正規化與排除）"""

    candidates: pd.DataFrame = log[log["row_count"] > 0]
    if candidates.empty:
        logger.warning("[比對 3] 沒有寫入任何資料，略過抽樣")
        return True

    sample: pd.DataFrame = candidates.sample(
        n=min(SAMPLE_STOCK_DAYS, len(candidates)), random_state=seed
    )
    api: StockTickAPI = StockTickAPI(dao=dao)
    mismatched: List[str] = []
    for stock_id, trade_date in sample[["stock_id", "trade_date"]].itertuples(
        index=False
    ):
        day: datetime.date = datetime.date.fromisoformat(trade_date)
        csv_path: Path = source_dir / f"{stock_id}.csv"
        time_column: str = detect_time_column(csv_path)
        parts: List[pd.DataFrame] = [
            part[part[time_column].str.startswith(trade_date)]
            for part in pd.read_csv(
                csv_path, dtype=str, keep_default_na=False, chunksize=READ_CHUNK_ROWS
            )
        ]
        expected: pd.DataFrame = add_trade_date_and_seq(
            normalize_tick_frame(pd.concat(parts, ignore_index=True), stock_id)
        )
        expected, _, _ = filter_tick_rows(
            expected,
            price_dao.get_stock_trading_days(stock_id, day, day),
            set(price_dao.get_trading_days(day, day)),
        )
        expected = expected.sort_values(["time", "seq"]).loc[:, list(TICK_COLUMNS)]
        actual: pd.DataFrame = api.get_stock_ticks(stock_id, day, day)
        try:
            pd.testing.assert_frame_equal(
                expected.reset_index(drop=True),
                actual.reset_index(drop=True),
                check_dtype=False,
            )
        except AssertionError as error:
            mismatched.append(f"{stock_id} {trade_date}：{str(error)[:200]}")

    logger.info(f"[比對 3] 抽樣 {len(sample)} 組逐列比對：不符 {len(mismatched)} 組")
    for item in mismatched:
        logger.error(f"  {item}")
    return not mismatched


def report_daily_volume(
    dao: StockTickDAO, price_dao: StockPriceDAO, log: pd.DataFrame, seed: int
) -> None:
    """抽樣交易日，比對 tick 成交量加總與 `price` 表成交股數 ÷ 1000（只取兩邊都有的股票）"""

    days: List[str] = sorted(log["trade_date"].unique())
    if not days:
        return
    rng: random.Random = random.Random(seed)
    for trade_date in sorted(rng.sample(days, min(SAMPLE_VOLUME_DAYS, len(days)))):
        with dao.conn.transaction():
            tick_volume: Dict[str, int] = dict(
                dao.conn.execute(
                    f'SELECT stock_id, sum(volume) FROM "{dao.schema}"."{dao.TABLE_NAME}" '
                    "WHERE time >= %s AND time < %s::date + 1 GROUP BY stock_id",
                    (trade_date, trade_date),
                ).fetchall()
            )
        price: pd.DataFrame = price_dao.query_df(
            "SELECT stock_id, 成交股數 FROM price WHERE date = ?", (trade_date,)
        )
        price_lots: Dict[str, float] = {
            stock_id: shares / 1000
            for stock_id, shares in zip(price["stock_id"], price["成交股數"])
            if stock_id in tick_volume
        }
        tick_total: int = sum(tick_volume[s] for s in price_lots)
        price_total: float = sum(price_lots.values())
        ratio: float = tick_total / price_total if price_total else float("nan")
        logger.info(
            f"[比對 4] {trade_date}：{len(price_lots)} 檔，tick 加總 {tick_total:,} 張、"
            f"price 表 {price_total:,.0f} 張（比值 {ratio:.4f}）"
        )


# === 計畫 ===
def print_plan(args: argparse.Namespace, dao: StockTickDAO) -> None:
    """只列計畫：來源、工作目錄空間、推估耗時與資料庫現況"""

    csv_files: List[Path] = sorted(args.source_dir.glob("*.csv"))
    total_bytes: int = sum(f.stat().st_size for f in csv_files)
    estimated_rows: float = total_bytes / CSV_BYTES_PER_ROW
    free_bytes: int = shutil.disk_usage(args.work_dir.parent).free
    # 只讀：表還沒建就報 0，不替使用者建表（列計畫不該有任何寫入）
    db_rows: int = 0
    log_rows: int = 0
    if dao.table_exists():
        with dao.conn.transaction():
            db_rows = dao.conn.execute(
                f'SELECT count(*) FROM "{dao.schema}"."{dao.TABLE_NAME}"'
            ).fetchone()[0]
            log_rows = dao.conn.execute(
                f'SELECT count(*) FROM "{dao.schema}"."{dao.LOAD_LOG_TABLE_NAME}"'
            ).fetchone()[0]

    logger.info(
        f"來源：{args.source_dir}（{len(csv_files)} 檔、{total_bytes / 1e9:.1f} GB）"
    )
    logger.info(f"區間：{args.start_date or '最早'} ～ {args.end_date or '最晚'}")
    logger.info(
        f"工作目錄：{args.work_dir}（切檔峰值約 {total_bytes / 1e9:.1f} GB，"
        f"所在磁碟剩 {free_bytes / 1e9:.0f} GB）"
    )
    logger.info(
        f"推估：約 {estimated_rows / 1e8:.1f} 億列、寫入約 "
        f"{estimated_rows / PILOT_ROWS_PER_SEC / 3600:.1f} 小時（不含切檔與比對）"
    )
    logger.info(
        f"資料庫 {dao.schema}：stock_tick {db_rows:,} 列、load_log {log_rows:,} 組"
    )
    logger.info("未帶 --apply，只列計畫，沒有寫入任何東西")


def main() -> None:
    """列計畫；`--apply` 匯入並比對；`--verify-only` 只比對"""

    args: argparse.Namespace = parse_arguments()
    LogManager.setup_logger("tick_history_import.log")
    if not args.source_dir.is_dir():
        sys.exit(f"找不到來源目錄：{args.source_dir}")
    if not args.price_db.exists():
        sys.exit(f"找不到日 K 資料庫：{args.price_db}")

    dao: StockTickDAO = StockTickDAO(schema=args.schema)
    price_dao: StockPriceDAO = StockPriceDAO(db_path=args.price_db, read_only=True)
    try:
        if not args.apply and not args.verify_only:
            print_plan(args, dao)
            return

        dao.create_tables()

        source_counts: Optional[pd.DataFrame] = load_split_if_reusable(
            args.work_dir, args.source_dir, args.start_date, args.end_date
        )
        if args.verify_only:
            if source_counts is None:
                sys.exit("找不到切檔紀錄（source_counts.csv），請先以 --apply 匯入")
        else:
            if source_counts is None or not args.resume:
                logger.info("[切檔] 開始")
                source_counts = split_by_chunk(
                    args.source_dir, args.work_dir, args.start_date, args.end_date
                )
            loader: StockTickLoader = StockTickLoader(dao=dao, price_dao=price_dao)
            dao.pause_compression_policy()
            try:
                failed: List[str] = import_chunks(
                    args.work_dir, dao, loader, args.resume
                )
            finally:
                dao.resume_compression_policy()
            if failed:
                logger.error(
                    f"[匯入] {len(failed)} 個切檔失敗（保留在工作目錄），"
                    f"修正後以 --resume 重跑：{failed[:20]}"
                )

        if not verify(dao, price_dao, source_counts, args.source_dir, args.seed):
            sys.exit(1)
    finally:
        dao.close()
        price_dao.close()


if __name__ == "__main__":
    main()
