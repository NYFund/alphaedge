import datetime
from typing import Optional

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import API_LOG_FILE_LEVEL, API_LOGS_DIR_PATH, TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.utils.constant import FuturesAdjustMethod, FuturesRollRule, FuturesSession
from core.utils.log_manager import LogManager

"""
Futures Continuous API: query futures_continuous table through FuturesContinuousDAO

**與 `FuturesPriceAPI` 的分工**：那一份回傳當日**所有**掛牌合約（一天多列），
本份回傳**一天一列**的連續序列——換月已經接好、價差已經調整掉。

⚠️ **一定要指定調整方式與換月規則**：同一天在同一張表裡有三種調整方式 ×
三種換月規則的列，價格口徑各不相同。混著看等於把三條不同的曲線疊在一起。

⚠️ **這是衍生表**：`--target futures_continuous` 跑過才有資料，且只涵蓋建過的
那幾組設定。查不到時一律回空表，由呼叫端決定要退回什麼，**不要自行換一組設定
補上去**——那會讓呼叫端拿到一條它沒有要求的曲線而毫無徵兆。
"""


class FuturesContinuousAPI(BaseDataAPI):
    """Futures Continuous API"""

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 由呼叫端傳入共用連線；未指定時自行建立
        self.conn: Optional[DBConnection] = conn
        self.owns_conn: bool = conn is None

        # SQL 一律在 DAO；連線所有權仍由本 API 持有（DAO 不擁有），`close()` 沿用基底行為
        self.dao: Optional[FuturesContinuousDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Data API"""

        if self.owns_conn:
            self.conn = connect_sqlite(TW_FUTURES_DB_PATH)
        self.dao = FuturesContinuousDAO(conn=self.conn)
        LogManager.setup_logger(
            "futures_continuous_api.log",
            log_dir=API_LOGS_DIR_PATH,
            level=API_LOG_FILE_LEVEL,
        )

    def get_series(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: FuturesSession = FuturesSession.DAY,
        method: FuturesAdjustMethod = FuturesAdjustMethod.BACKWARD,
        roll_rule: FuturesRollRule = FuturesRollRule.LAST_TRADING_DAY,
    ) -> pd.DataFrame:
        """
        - Description:
            取指定設定的連續合約序列（一天一列）
        - Parameters:
            - product: str
                商品代碼
            - start_date / end_date: datetime.date
                日期範圍（含兩端）
            - session: FuturesSession
                交易時段；連續合約目前只建日盤
            - method: FuturesAdjustMethod
                調整方式；預設 `BACKWARD`（價差連續，點數可直接相減）
            - roll_rule: FuturesRollRule
                換月規則，應與策略實際轉倉的規則一致
        - Return:
            - pd.DataFrame
                依日期排序的序列；**表不存在或查無該組設定時為空表**
        """

        # 表不存在＝尚未跑過 `--target futures_continuous`，那是「還沒有資料」
        # 不是查詢寫錯（全新環境本來就會走到這裡），語意與其他 API 一致
        if not self.dao.table_exists():
            return pd.DataFrame()

        return self.dao.get_series(
            product,
            session.value,
            method.value,
            roll_rule.value,
            start_date,
            end_date,
        )

    def get_close_series(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: FuturesSession = FuturesSession.DAY,
        method: FuturesAdjustMethod = FuturesAdjustMethod.BACKWARD,
        roll_rule: FuturesRollRule = FuturesRollRule.LAST_TRADING_DAY,
    ) -> pd.Series:
        """
        - Description:
            取連續合約的收盤價序列，index 為 `datetime.date`

            對標曲線只要收盤價，`get_series()` 的其餘欄位對它沒有意義。
        - Parameters:
            - 同 `get_series()`
        - Return:
            - pd.Series
                index 為交易日、值為收盤價；查無資料時為空 Series
        """

        df: pd.DataFrame = self.get_series(
            product,
            start_date,
            end_date,
            session=session,
            method=method,
            roll_rule=roll_rule,
        )
        if df.empty:
            return pd.Series(dtype=float)

        series: pd.Series = df[FuturesPriceColumn.CLOSE.value].astype(float)
        series.index = pd.to_datetime(df["date"]).dt.date
        return series
