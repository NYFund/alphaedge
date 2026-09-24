import datetime
from typing import Optional

import pandas as pd
from loguru import logger

try:
    import dolphindb as ddb
except ModuleNotFoundError:
    print("Warning: dolphindb module is not installed")

from core.api.base import BaseDataAPI
from core.config import (
    API_LOG_FILE_LEVEL,
    API_LOGS_DIR_PATH,
    DDB_HOST,
    DDB_PASSWORD,
    DDB_PORT,
    DDB_USER,
    TICK_DB_PATH,
    TICK_TABLE_NAME,
    require_tick_db_path,
)
from core.utils.log_manager import LogManager

"""Tick data API: query DolphinDB tick data"""


class StockTickAPI(BaseDataAPI):
    """Tick data API"""

    def __init__(self) -> None:
        self.default_stock_id: str = "2330"
        self.query_start_date: str = "2024.05.10"
        self.query_end_date: str = "2024.05.10"

        self.session: Optional[ddb.session] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        # `DDB_PATH` 沒設定時會拼出 `"NonetickDB"` 這種看似路徑的字串，
        # 讓錯誤訊息指不到真正的原因；在連線之前就攔下來
        require_tick_db_path()

        self.session: ddb.session = ddb.session()
        self.session.connect(DDB_HOST, DDB_PORT, DDB_USER, DDB_PASSWORD)

        if self.session.existsDatabase(TICK_DB_PATH):
            logger.info("* Database exists!")

            # TSDBCacheEngineSize 設為 2GB（須小於 maxMemSize * 0.75）
            script: str = """
            memSize = 2
            setTSDBCacheEngineSize(memSize)
            print("TSDBCacheEngineSize: " + string(getTSDBCacheEngineSize() / pow(1024, 3)) + "GB")
            """
            self.session.run(script)
        else:
            print("* Database doesn't exist!")

        LogManager.setup_logger(
            "stock_tick_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有個股各自排序好 tick 資料（個股沒有混在一起排序）"""

        if start_date > end_date:
            return pd.DataFrame()

        start_date_str: str = start_date.strftime("%Y.%m.%d")
        end_date_str: str = (end_date + datetime.timedelta(days=1)).strftime("%Y.%m.%d")
        script: str = f"""
        db = database("{TICK_DB_PATH}")
        table = loadTable(db, "{TICK_TABLE_NAME}")
        select * from table
        where time between nanotimestamp({start_date_str}):nanotimestamp({end_date_str})
        """
        tick: pd.DataFrame = self.session.run(script)
        return tick

    def get_ordered_ticks(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得排序好的 tick 資料（所有個股混在一起以時間排序，模擬市場盤中情形）"""

        if start_date > end_date:
            return pd.DataFrame()

        start_date_str: str = start_date.strftime("%Y.%m.%d")
        end_date_str: str = (end_date + datetime.timedelta(days=1)).strftime("%Y.%m.%d")
        script: str = f"""
        db = database("{TICK_DB_PATH}")
        table = loadTable(db, "{TICK_TABLE_NAME}")
        select * from table
        where time between nanotimestamp({start_date_str}):nanotimestamp({end_date_str}) order by time
        """
        tick: pd.DataFrame = self.session.run(script)
        return tick

    def get_stock_ticks(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得個股 tick 資料"""

        if start_date > end_date:
            return pd.DataFrame()

        start_date_str: str = start_date.strftime("%Y.%m.%d")
        end_date_str: str = (end_date + datetime.timedelta(days=1)).strftime("%Y.%m.%d")
        script: str = f"""
        db = database("{TICK_DB_PATH}")
        table = loadTable(db, "{TICK_TABLE_NAME}")
        select * from table
        where stock_id=`{stock_id} and time between nanotimestamp({start_date_str}):nanotimestamp({end_date_str})
        """
        tick: pd.DataFrame = self.session.run(script)
        return tick

    def get_last_tick(
        self,
        stock_id: str,
        date: datetime.date,
    ) -> pd.DataFrame:
        """取得當日最後一筆 tick"""

        tick: pd.DataFrame = self.get_stock_ticks(stock_id, date, date)

        if tick.empty:
            return pd.DataFrame()
        return tick.iloc[-1:]
