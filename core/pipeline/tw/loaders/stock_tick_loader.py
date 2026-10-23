import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd
from loguru import logger

from core.config import TICK_DOWNLOADS_PATH, TW_STOCK_DB_PATH
from core.dao.timescale import tick_db_error_types
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.dao.tw.stock_tick_dao import StockTickDAO
from core.pipeline.shared.base_loader import BaseDataLoader

"""
台股 Tick Loader（TimescaleDB）

把 `{stock_id}.csv` 正規化、依規則排除後，以「股票 × 交易日」為單位寫進 `stock_tick`，
並在 `stock_tick_load_log` 登記來源列數與寫入列數。SQL 全在 `StockTickDAO`，本檔不碰驅動。

**讀得懂兩種 CSV**：
- cleaner 格式（每日更新產生）：`stock_id,time,close,...`，欄序固定。
- 歷史格式（2020-04～2024-05 的存檔）：時間欄叫 `ts`、沒有 `stock_id` 欄（代號只在檔名）、
  8 個欄位有 31 種排列、部分檔案的整數寫成 `105.0`、興櫃時期的時間只到 millisecond。
正規化與排除都在這裡做，日常更新與歷史匯入才會是同一套規則、同一個對帳口徑。

**排除規則**（各規則的列數記進 log，`source_rows - row_count` 就是排除的列數）：
- `close = 0` 的整列全零資料、`volume < 0` 的資料——兩者都是來源端的壞列。
- **興櫃時期**：當天 `price` 表沒有這檔股票就整天排除。興櫃以「股」為單位、交易到 15:00 之後，
  混進來成交量會差上千倍；`price` 只收上市櫃，是唯一可靠的判準（時間精度不是：
  有些檔案的興櫃時期已經是 microsecond 格式）。
- **`price` 表整天都沒有資料時不是排除，是失敗**：那代表日 K 還沒更新，不是興櫃。
  若照興櫃排除並登記成已載入，這一天之後就再也不會補。
"""

# 正規化後的欄位與順序；與 cleaner 輸出一致
TICK_COLUMNS: Tuple[str, ...] = (
    "stock_id",
    "time",
    "close",
    "volume",
    "bid_price",
    "bid_volume",
    "ask_price",
    "ask_volume",
    "tick_type",
)
PRICE_COLUMNS: Tuple[str, ...] = ("close", "bid_price", "ask_price")
INT_COLUMNS: Tuple[str, ...] = ("volume", "bid_volume", "ask_volume", "tick_type")

# 歷史格式的時間欄名
HISTORY_TIME_COLUMN: str = "ts"

# 價格的小數位數：台股最小跳動 0.01，多出來的位數是浮點誤差（`6.5600000000000005`）
PRICE_DECIMALS: int = 2

# 排除規則的名稱（log 與統計用）
EXCLUDE_ZERO_CLOSE: str = "zero_close"
EXCLUDE_NEGATIVE_VOLUME: str = "negative_volume"
EXCLUDE_NOT_LISTED: str = "not_listed"


def normalize_tick_frame(raw: pd.DataFrame, stock_id: str) -> pd.DataFrame:
    """
    - Description:
        把 cleaner 格式或歷史格式的原始資料轉成 `TICK_COLUMNS` 的固定欄位與型別

        **一律依欄名取欄**：歷史格式的欄序每檔不一。任何一格轉不過去就整檔失敗，
        不默默丟列——丟掉的列會讓 `source_rows` 對不上，卻沒有任何規則可以解釋。
    - Parameters:
        - raw: pd.DataFrame
            以字串讀入的原始 CSV（`dtype=str`）
        - stock_id: str
            股票代號（檔名）；歷史格式沒有 `stock_id` 欄時用它補上
    - Return:
        - pd.DataFrame
            `TICK_COLUMNS` 欄位：`stock_id` 為 str、`time` 為 `datetime64`（naive）、
            價格為四捨五入到 2 位的 float、量與 `tick_type` 為 int64；列序與原始檔相同
    - Raise:
        - ValueError
            缺欄、時間無法解析、數值無法轉換或整數欄帶小數
    """

    df: pd.DataFrame = raw
    if "time" not in df.columns and HISTORY_TIME_COLUMN in df.columns:
        df = df.rename(columns={HISTORY_TIME_COLUMN: "time"})
    if "stock_id" not in df.columns:
        df = df.assign(stock_id=stock_id)

    missing: List[str] = [column for column in TICK_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"{stock_id}：缺少欄位 {missing}（現有 {list(raw.columns)}）")

    normalized: pd.DataFrame = pd.DataFrame(
        {
            "stock_id": df["stock_id"].astype(str),
            # ISO8601：microsecond（26 字元）與 millisecond（23 字元）都吃得下
            "time": pd.to_datetime(df["time"], format="ISO8601"),
        }
    )
    for column in PRICE_COLUMNS:
        normalized[column] = pd.to_numeric(df[column]).round(PRICE_DECIMALS)
    for column in INT_COLUMNS:
        # 先轉 float 再轉 int：部分歷史檔把整數寫成 `105.0`
        values: pd.Series = pd.to_numeric(df[column])
        fractional: pd.Series = values != values.round()
        if values.isna().any() or fractional.any():
            raise ValueError(f"{stock_id}：{column} 有空值或非整數")
        normalized[column] = values.astype("int64")

    return normalized.loc[:, list(TICK_COLUMNS)]


def add_trade_date_and_seq(df: pd.DataFrame) -> pd.DataFrame:
    """
    - Description:
        補上 `trade_date` 與 `seq`（同一股票、同一交易日內的原始列序，從 0 起算）

        **必須在排除之前算**：`seq` 記的是原始檔的列序，排除後留下的缺號正好可以
        回頭對照 CSV。同一天內的原始列序不一定依時間排序（盤後列可能排在收盤列前面），
        `seq` 只用來讓同一時間戳記的多筆成交有可重現的順序。
    - Parameters:
        - df: pd.DataFrame
            `normalize_tick_frame()` 的結果
    - Return:
        - pd.DataFrame
            多了 `trade_date`（`datetime.date`）與 `seq`（int64）的新表
    """

    result: pd.DataFrame = df.assign(trade_date=df["time"].dt.date)
    result["seq"] = result.groupby(["stock_id", "trade_date"]).cumcount()
    return result


def filter_tick_rows(
    df: pd.DataFrame,
    listed_days: Set[datetime.date],
    trading_days: Set[datetime.date],
) -> Tuple[pd.DataFrame, Dict[str, int], List[datetime.date]]:
    """
    - Description:
        依排除規則過濾一檔股票的資料（規則見模組說明）
    - Parameters:
        - df: pd.DataFrame
            `add_trade_date_and_seq()` 的結果（單一股票）
        - listed_days: Set[datetime.date]
            這檔股票在 `price` 表有日 K 的日子
        - trading_days: Set[datetime.date]
            `price` 表有任何資料的日子（交易日曆）
    - Return:
        - Tuple[pd.DataFrame, Dict[str, int], List[datetime.date]]
            留下的列（不含失敗日）、各規則排除的列數、失敗日（`price` 表整天沒資料，已排序）
    """

    trade_dates: pd.Series = df["trade_date"]
    failed_mask: pd.Series = ~trade_dates.isin(trading_days)
    failed_days: List[datetime.date] = sorted(set(trade_dates[failed_mask]))

    candidate: pd.DataFrame = df[~failed_mask]
    not_listed: pd.Series = ~candidate["trade_date"].isin(listed_days)
    zero_close: pd.Series = ~not_listed & (candidate["close"] == 0)
    negative_volume: pd.Series = ~not_listed & ~zero_close & (candidate["volume"] < 0)

    stats: Dict[str, int] = {
        EXCLUDE_NOT_LISTED: int(not_listed.sum()),
        EXCLUDE_ZERO_CLOSE: int(zero_close.sum()),
        EXCLUDE_NEGATIVE_VOLUME: int(negative_volume.sum()),
    }
    kept: pd.DataFrame = candidate[~(not_listed | zero_close | negative_volume)]
    return kept, stats, failed_days


class StockTickLoader(BaseDataLoader):
    """
    - Description:
        台股 tick 入庫：CSV → 正規化 → 排除 → 逐日「刪除後重寫」進 TimescaleDB
    """

    SOURCE: str = "tick"

    def __init__(
        self,
        dao: Optional[StockTickDAO] = None,
        price_dao: Optional[StockPriceDAO] = None,
    ) -> None:
        """
        - Description:
            建立 loader；兩個 DAO 都可由呼叫端傳入共用，未傳就自行建立並在 `disconnect()` 關閉
        - Parameters:
            - dao: Optional[StockTickDAO]
                tick 的 TimescaleDB DAO
            - price_dao: Optional[StockPriceDAO]
                判斷上市櫃交易日用的日 K DAO（唯讀）
        """

        self.price_dao: Optional[StockPriceDAO] = price_dao
        self.owns_price_dao: bool = price_dao is None
        super().__init__(dao)

    def price_db_path(self) -> Path:
        """日 K 資料庫路徑；寫成方法讓測試在呼叫當下改寫模組常數（理由見 `db_path()`）"""

        return TW_STOCK_DB_PATH

    def downloads_path(self) -> Optional[Path]:
        """tick updater 的工作目錄"""

        return TICK_DOWNLOADS_PATH

    def connect(self) -> None:
        """建立（或沿用）tick DAO 與日 K DAO"""

        if self.dao is None:
            self.dao = StockTickDAO()
            self.owns_dao = True
        self.conn = self.dao.conn

        if self.price_dao is None:
            self.price_dao = StockPriceDAO(db_path=self.price_db_path(), read_only=True)
            self.owns_price_dao = True

    def disconnect(self) -> None:
        """關閉自己建立的 DAO；共用的由建立者關閉"""

        if self.owns_price_dao and self.price_dao is not None:
            self.price_dao.close()
            self.price_dao = None
        super().disconnect()

    def create_db(self) -> None:
        """建立 `stock_tick` hypertable、壓縮設定與 `load_log`（可重跑）"""

        self.dao.create_tables()

    def create_missing_tables(self) -> None:
        """
        確保資料表存在

        直接重跑 `create_tables()` 而不先查存在與否：每一句都是 `IF NOT EXISTS`，
        表已存在、甚至已有壓縮 chunk 時重跑都不會改到資料；只查 `stock_tick`
        反而會漏掉「hypertable 在、`load_log` 不在」這種半套狀態。
        """

        self.create_db()

    def load_csv(self, csv_path: Path) -> int:
        """
        - Description:
            載入一個 CSV（一檔股票、任意天數）；逐日一個交易
        - Parameters:
            - csv_path: Path
                CSV 路徑；檔名（不含副檔名）即股票代號
        - Return:
            - int
                寫入的總列數
        - Raise:
            - ValueError
                格式錯誤（整檔不寫），或有交易日在 `price` 表整天沒資料
                （其餘日子已寫入，失敗日沒有 `load_log`、下次會重做）
        """

        stock_id: str = csv_path.stem
        # 全部以字串讀入：代號的前導 0 要留著，數值的轉換與檢查統一在正規化做。
        # `keep_default_na=False`：空格保持空字串，轉數值時報錯，而不是默默變成 NaN
        raw: pd.DataFrame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
        if raw.empty:
            logger.warning(f"[tick] {csv_path.name} 沒有資料列，略過")
            return 0

        df: pd.DataFrame = add_trade_date_and_seq(normalize_tick_frame(raw, stock_id))
        source_rows: pd.Series = df.groupby("trade_date").size()
        first_day: datetime.date = source_rows.index.min()
        last_day: datetime.date = source_rows.index.max()

        listed_days: Set[datetime.date] = self.price_dao.get_stock_trading_days(
            stock_id, first_day, last_day
        )
        trading_days: Set[datetime.date] = set(
            self.price_dao.get_trading_days(first_day, last_day)
        )
        kept, stats, failed_days = filter_tick_rows(df, listed_days, trading_days)

        kept_by_day: Dict[datetime.date, pd.DataFrame] = {
            trade_date: rows for trade_date, rows in kept.groupby("trade_date")
        }
        empty_day: pd.DataFrame = kept.iloc[0:0]
        written: int = 0
        for trade_date, rows in source_rows.items():
            if trade_date in failed_days:
                continue
            written += self.dao.replace_day(
                stock_id,
                trade_date,
                kept_by_day.get(trade_date, empty_day),
                int(rows),
                csv_path.name,
            )

        excluded: Dict[str, int] = {rule: n for rule, n in stats.items() if n}
        logger.info(
            f"[tick] {csv_path.name}：{len(source_rows) - len(failed_days)} 天、"
            f"寫入 {written:,} 列" + (f"、排除 {excluded}" if excluded else "")
        )
        if failed_days:
            raise ValueError(
                f"{csv_path.name}：{len(failed_days)} 個交易日在 price 表整天沒有資料，"
                f"未寫入（日 K 可能還沒更新）：{failed_days[:5]}"
            )
        return written

    def is_fully_loaded(self, csv_path: Path) -> bool:
        """
        - Description:
            CSV 裡的每個交易日都已登記在 `load_log`、且來源列數一致

            updater 開工前據此刪掉已入庫的 CSV。**只比對列數、不比對內容**：
            同一天的檔案內容若有變，列數幾乎必然跟著變；真的只是值不同的話，
            留著它也只是下次再寫一次（刪除後重寫是冪等的），不會掉資料。
        - Parameters:
            - csv_path: Path
                CSV 路徑；檔名即股票代號
        - Return:
            - bool
                全部交易日都已入庫時為 True；空檔、有任何一天沒登記或列數不同時為 False
        """

        time_columns: Set[str] = {"time", HISTORY_TIME_COLUMN}
        # 只讀時間欄：單檔動輒數十萬列，整份讀進來純屬浪費
        times: pd.DataFrame = pd.read_csv(
            csv_path, usecols=lambda column: column in time_columns, dtype=str
        )
        if times.empty or times.shape[1] != 1:
            return False

        days: pd.Series = times.iloc[:, 0].str.slice(0, 10)
        csv_rows: Dict[datetime.date, int] = {
            datetime.date.fromisoformat(day): int(count)
            for day, count in days.value_counts().items()
        }
        loaded_rows: Dict[datetime.date, int] = self.dao.get_source_rows(csv_path.stem)
        return all(loaded_rows.get(day) == count for day, count in csv_rows.items())

    def add_to_db(
        self, remove_files: bool = False, dir_path: Optional[Path] = None
    ) -> None:
        """
        - Description:
            逐檔載入資料夾內的 CSV；單檔失敗不中斷整批，全部跑完由 `finish_load()` 彙報
        - Parameters:
            - remove_files: bool
                全部成功後刪除來源資料夾；只允許用在 updater 的工作目錄
            - dir_path: Optional[Path]
                來源資料夾；None 取 updater 的工作目錄。抽樣試點與歷史匯入指定其他資料夾
        - Raise:
            - ValueError
                對指定的資料夾要求刪檔（歷史存檔是唯一的原始資料，不可順手刪掉）
            - DataLoadError
                有任何檔案失敗
        """

        if remove_files and dir_path is not None:
            raise ValueError(
                "remove_files 只能用在 updater 的工作目錄，不可刪除指定的資料夾"
            )

        source_dir: Path = dir_path if dir_path is not None else self.downloads_path()
        csv_files: List[Path] = sorted(source_dir.glob("*.csv"))
        logger.info(f"[tick] {source_dir}：{len(csv_files)} 個 CSV")

        self.create_missing_tables()
        succeeded: int = 0
        failed_files: List[str] = []
        for csv_path in csv_files:
            # 逐檔隔離：一個壞檔不擋其他檔；只收格式、讀檔與資料庫錯誤，其他錯誤照常往外拋
            try:
                self.load_csv(csv_path)
                succeeded += 1
            except (ValueError, OSError, *tick_db_error_types()) as error:
                logger.error(f"[tick] {csv_path.name} 入庫失敗：{error}")
                failed_files.append(csv_path.name)

        self.finish_load(
            source=self.SOURCE,
            succeeded=succeeded,
            failed_files=failed_files,
            remove_files=remove_files,
            downloads_path=source_dir,
        )
