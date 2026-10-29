import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd
from loguru import logger

from core.dao.timescale import connect_tick_db
from core.utils.constant.market import ETF_PRICE_TICK_TABLE, PRICE_TICK_TABLE

"""
台股逐筆成交的報價價差彙總：每檔每日一列，按月快取

**只看連續競價**：開盤集合競價（09:00:00 的第一筆撮合）、收盤集合競價（13:25 起）與
盤後定價（14:30）的那幾筆不是在「委買委賣之間吃一檔」成交，拿它們的委買委賣算價差沒有意義。
09:00:05 之前一律排除，第一筆撮合的時間戳記會落在 09:00:00 後幾十毫秒內。

**鎖漲跌停與單邊無報價也排除**：任一側為 0、或賣價不高於買價的列，價差不是流動性的反映。

半價差 ＝ (賣價 − 買價) ÷ 中價 ÷ 2，換成 bps——以市價單吃一檔的成本就是半個價差。
另外記下價差是幾個跳動單位：價差大多被跳動單位卡在 1 檔的話，成本主要由股價與檔位表
決定，不需要逐標的的表。
"""


def build_tick_size_case(table: List[Tuple[float, float]]) -> str:
    """把檔位表 `(價格上限, 檔位)` 組成 SQL 的 CASE；最後一段上限是無限大，當成 ELSE"""

    branches: List[str] = [
        f"WHEN bid_price < {upper} THEN {tick}"
        for upper, tick in table
        if upper != float("inf")
    ]
    return f"CASE {' '.join(branches)} ELSE {table[-1][1]} END"


# 跳動單位直接由 `core` 的檔位表組成（ETF 以代號 `00` 開頭判定，與 `StockUtils.get_tick_table()` 相同）。
# 彙總在資料庫裡做，不把十億列搬進 pandas
TICK_SIZE_SQL: str = (
    f"CASE WHEN LEFT(stock_id, 2) = '00' THEN {build_tick_size_case(ETF_PRICE_TICK_TABLE)} "
    f"ELSE {build_tick_size_case(PRICE_TICK_TABLE)} END"
)

DAILY_SPREAD_SQL: str = f"""
SELECT
    stock_id,
    COUNT(*) AS n_ticks,
    SUM(volume) AS lots,
    AVG(half_bps) AS avg_half_bps,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY half_bps) AS med_half_bps,
    SUM(half_bps * volume) / NULLIF(SUM(volume), 0) AS vw_half_bps,
    AVG(spread_ticks) AS avg_spread_ticks,
    AVG(CASE WHEN spread_ticks < 1.5 THEN 1.0 ELSE 0.0 END)::float8 AS one_tick_ratio,
    AVG(tick_floor_half_bps) AS avg_floor_half_bps
FROM (
    SELECT
        stock_id,
        volume,
        (ask_price - bid_price) / ((ask_price + bid_price) / 2) / 2 * 10000 AS half_bps,
        (ask_price - bid_price) / ({TICK_SIZE_SQL}) AS spread_ticks,
        ({TICK_SIZE_SQL}) / ((ask_price + bid_price) / 2) / 2 * 10000 AS tick_floor_half_bps
    FROM public.stock_tick
    WHERE time >= %s AND time < %s
      AND bid_price > 0 AND ask_price > bid_price
      AND time::time >= '09:00:05' AND time::time < '13:25:00'
) AS t
GROUP BY stock_id
"""


def aggregate_day(cursor: Any, date: datetime.date) -> pd.DataFrame:
    """
    - Description:
        一個交易日全市場的每檔價差彙總；休市日回空表
    - Parameters:
        - cursor: Any
            psycopg 的 cursor
        - date: datetime.date
            交易日
    - Return:
        - pd.DataFrame
            每檔一列，欄位見 `DAILY_SPREAD_SQL`，另加 `date`
    """

    cursor.execute(DAILY_SPREAD_SQL, (date, date + datetime.timedelta(days=1)))
    rows: List[Any] = cursor.fetchall()
    if not rows:
        return pd.DataFrame()
    columns: List[str] = [description.name for description in cursor.description]
    df: pd.DataFrame = pd.DataFrame(rows, columns=columns)
    df.insert(0, "date", date)
    return df


def aggregate_month(
    year: int, month: int, cache_dir: Path, refresh: bool = False
) -> Optional[Path]:
    """
    - Description:
        彙總一個月並寫成 parquet；已存在就略過（中斷後重跑只補沒做完的月份）
    - Parameters:
        - year: int
            西元年
        - month: int
            月份
        - cache_dir: Path
            快取目錄
        - refresh: bool
            True 時重算已存在的月份
    - Return:
        - Optional[Path]
            寫出的檔案；該月沒有任何 tick 時為 None
    """

    path: Path = cache_dir / f"{year:04d}-{month:02d}.parquet"
    if path.exists() and not refresh:
        return path

    first: datetime.date = datetime.date(year, month, 1)
    after: datetime.date = (first + datetime.timedelta(days=32)).replace(day=1)
    frames: List[pd.DataFrame] = []

    with connect_tick_db(autocommit=True) as conn:
        cursor: Any = conn.cursor()
        day: datetime.date = first
        while day < after:
            if day.weekday() < 5:
                frame: pd.DataFrame = aggregate_day(cursor, day)
                if not frame.empty:
                    frames.append(frame)
            day += datetime.timedelta(days=1)

    if not frames:
        logger.info(f"{year}-{month:02d} 沒有 tick，略過")
        return None

    cache_dir.mkdir(parents=True, exist_ok=True)
    monthly: pd.DataFrame = pd.concat(frames, ignore_index=True)
    monthly.to_parquet(path, index=False)
    logger.info(
        f"{year}-{month:02d}：{len(frames)} 個交易日、{len(monthly)} 列 → {path.name}"
    )
    return path


def aggregate_range(
    start: datetime.date, end: datetime.date, cache_dir: Path, refresh: bool = False
) -> List[Path]:
    """逐月彙總 `[start, end]` 涵蓋的月份，回傳寫出（或已存在）的檔案"""

    paths: List[Path] = []
    year: int = start.year
    month: int = start.month
    while (year, month) <= (end.year, end.month):
        path: Optional[Path] = aggregate_month(year, month, cache_dir, refresh)
        if path is not None:
            paths.append(path)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return paths
