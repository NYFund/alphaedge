from pathlib import Path
from typing import List

import pandas as pd
from loguru import logger

from core.dao.connection import DBConnection
from core.dao.tw.broker_trading_dao import BrokerTradingDAO
from core.pipeline.shared.base_loader import BaseDataLoader
from core.pipeline.utils import FinMindDataType
from core.pipeline.utils.exceptions import DataLoadError

"""
券商分點統計表的入庫：DataFrame 直入與 CSV 目錄批次兩條路徑

兩條路徑都交給資料庫的主鍵約束去重（`INSERT OR IGNORE`）。舊版先把「已存在的鍵」
查回記憶體再比對，再以 `DataFrame.to_sql` 追加——**`to_sql` 寫完會自行 commit**，
於是批次更新傳的 `commit=False` 從來沒有生效；且查詢失敗時 pandas 會對整條連線
`rollback()`，把尚未 commit 的前幾個組合一起丟掉。
"""


def select_columns(df: pd.DataFrame) -> pd.DataFrame:
    """依 crawler schema 註解的欄位順序排欄，只保留存在的欄位"""

    available_columns: List[str] = [
        col for col in BrokerTradingDAO.COLUMN_ORDER if col in df.columns
    ]
    return df[available_columns]


def drop_duplicate_keys(df: pd.DataFrame) -> pd.DataFrame:
    """
    去掉同一批內主鍵重複的列（保留第一筆）

    主鍵以字串比對：CSV 讀回來的 `stock_id`／`securities_trader_id` 可能被推斷成整數。
    """

    keys: pd.DataFrame = df[list(BrokerTradingDAO.PRIMARY_KEY_COLUMNS)].astype(str)
    return df[~keys.duplicated(keep="first")]


def load_from_dataframe(
    conn: DBConnection, df: pd.DataFrame, commit: bool = True
) -> int:
    """
    - Description:
        從 DataFrame 載入當日券商分點統計表資料到資料庫

        寫入包在 savepoint 內：本批寫到一半失敗時只回滾本批，同一交易內先前
        尚未 commit 的組合不受影響。
    - Parameters:
        - conn: DBConnection
            資料庫連線
        - df: pd.DataFrame
            要載入的 DataFrame
        - commit: bool
            是否在寫入後立即 commit；批次更新時由呼叫端傳 False 並定期 commit
    - Return:
        - int
            實際新寫入的列數（主鍵已存在的列不計）
    - Raise:
        - DataLoadError
            寫入失敗。**不再回 0**：舊版失敗後回 0，呼叫端把 0 當成「本批皆為重複」
            而回報 SUCCESS，於是入庫失敗被算成成功
    """

    if df is None or df.empty:
        logger.warning("DataFrame is empty, skipping load")
        return 0

    try:
        dao: BrokerTradingDAO = BrokerTradingDAO(conn=conn)

        original_count: int = len(df)
        deduped: pd.DataFrame = drop_duplicate_keys(df)
        if len(deduped) < original_count:
            logger.debug(
                f"Removed {original_count - len(deduped)} duplicate rows within DataFrame"
            )

        inserted: int
        with dao.savepoint("broker_trading_df"):
            inserted, _ = dao.insert_or_ignore(select_columns(deduped))
        if commit:
            dao.commit()

        if inserted == 0:
            logger.debug("All data already exists in database, skipping insert")
            return 0

        skipped_rows: int = original_count - inserted
        if skipped_rows > 0:
            logger.info(
                f"✅ Saved {inserted} new records to database "
                f"({skipped_rows} duplicates skipped)"
            )
        else:
            logger.info(f"✅ Saved {inserted} records to database")

        return inserted

    except Exception as e:
        logger.opt(exception=True).error(
            f"Error loading broker trading daily report from DataFrame: {e}",
        )
        raise DataLoadError("broker_trading", ["<dataframe>"], succeeded=0) from e


def load_from_files(conn: DBConnection, finmind_dir: Path) -> None:
    """
    - Description:
        載入當日券商分點統計表 CSV 到資料庫；有任何檔案失敗就拋 `DataLoadError`

        檔案結構：`broker_trading/{broker_id}/{stock_id}.csv`，會遍歷所有 broker_id
        資料夾。每個檔案包在 savepoint 內（壞檔整檔回滾），**全部處理完先 commit
        再彙報**：`finish_load()` 有失敗時會拋出，其他檔案已寫入的資料不可因此不落地。
    - Parameters:
        - conn: DBConnection
            資料庫連線
        - finmind_dir: Path
            downloads 底下的 finmind 目錄
    """

    data_type_dir: Path = finmind_dir / FinMindDataType.BROKER_TRADING.value.lower()

    if not data_type_dir.exists():
        logger.warning(f"Directory not found: {data_type_dir}")
        return

    # 遍歷所有 broker_id 資料夾
    broker_dirs: List[Path] = [d for d in data_type_dir.iterdir() if d.is_dir()]

    if not broker_dirs:
        logger.warning(f"No broker directories found in {data_type_dir}")
        return

    logger.info(f"Found {len(broker_dirs)} broker directories to process")

    dao: BrokerTradingDAO = BrokerTradingDAO(conn=conn)
    total_new_rows: int = 0
    total_skipped_rows: int = 0
    processed_files: int = 0
    skipped_files: int = 0
    failed_files: List[str] = []

    # 遍歷每個 broker_id 資料夾
    for broker_dir in broker_dirs:
        broker_id: str = broker_dir.name
        # 取得該 broker 資料夾下的所有 CSV 檔案
        csv_files: List[Path] = list(broker_dir.glob("*.csv"))

        for csv_path in csv_files:
            stock_id: str = csv_path.stem  # 檔名（不含副檔名）就是 stock_id
            processed_files += 1

            try:
                logger.debug(
                    f"Loading broker trading daily report from "
                    f"broker_id={broker_id}, stock_id={stock_id}..."
                )
                df: pd.DataFrame = pd.read_csv(csv_path, encoding="utf-8-sig")

                if df.empty:
                    logger.debug(f"Skipped {broker_id}/{stock_id}.csv (file is empty)")
                    skipped_files += 1
                    continue

                # 先處理同一個檔案內的重複資料
                original_count: int = len(df)
                df = drop_duplicate_keys(df)
                if len(df) < original_count:
                    logger.debug(
                        f"Removed {original_count - len(df)} duplicate rows "
                        f"within {broker_id}/{stock_id}.csv"
                    )

                inserted: int
                with dao.savepoint("broker_trading_file"):
                    inserted, _ = dao.insert_or_ignore(select_columns(df))

                if inserted == 0:
                    logger.debug(
                        f"Skipped {broker_id}/{stock_id}.csv (all data already exists)"
                    )
                    skipped_files += 1
                    continue

                skipped_rows: int = original_count - inserted
                total_new_rows += inserted
                total_skipped_rows += skipped_rows

                if skipped_rows > 0:
                    logger.debug(
                        f"Saved {broker_id}/{stock_id}.csv into database "
                        f"({inserted} new rows, {skipped_rows} skipped)"
                    )
                else:
                    logger.debug(
                        f"Saved {broker_id}/{stock_id}.csv into database "
                        f"({inserted} rows)"
                    )

            except Exception as e:
                # **失敗不再算成 skipped**：兩者混在一起時，
                # 「今天有 300 檔沒入庫」與「今天有 300 檔本來就沒新資料」
                # 在 log 裡長得一模一樣。單檔失敗仍不中止整批（其餘券商照跑），
                # 但跑完會由 `finish_load()` 拋出。
                logger.opt(exception=True).error(
                    f"Error loading {broker_id}/{stock_id}.csv: {e}",
                )
                failed_files.append(f"{broker_id}/{stock_id}.csv")
                continue

    dao.commit()

    # 輸出總結
    logger.info(
        f"Broker trading daily report loading finished. "
        f"Processed {processed_files} files, skipped {skipped_files} files, "
        f"failed {len(failed_files)} files. "
        f"Total: {total_new_rows} new rows, {total_skipped_rows} skipped rows"
    )

    BaseDataLoader.finish_load(
        source="broker_trading",
        succeeded=processed_files - skipped_files - len(failed_files),
        failed_files=failed_files,
        skipped_files=skipped_files,
    )
