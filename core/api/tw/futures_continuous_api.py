import datetime
from pathlib import Path
from typing import Type

import pandas as pd

from core.api.base import BaseDataAPI
from core.config import TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.dao.base import BaseDAO
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.utils.constant import FuturesAdjustMethod, FuturesRollRule, FuturesSession

"""
Futures Continuous API：經 `FuturesContinuousDAO` 查詢 `futures_continuous` 連續合約表

**與 `FuturesPriceAPI` 的分工**：那一份回傳當日**所有**掛牌合約（一天多列），
本份回傳**一天一列**的連續序列——換月已經接好、價差已經調整掉。

⚠️ **一定要指定調整方式與換月規則**：同一天在同一張表裡有三種調整方式 ×
三種換月規則的列，價格口徑各不相同。混著看等於把三條不同的曲線疊在一起。

⚠️ **這是衍生表**：`--target futures_continuous` 跑過才有資料，且只涵蓋建過的
那幾組設定。查不到時一律回空表，由呼叫端決定要退回什麼，**不要自行換一組設定
補上去**——那會讓呼叫端拿到一條它沒有要求的曲線而毫無徵兆。
"""


class FuturesContinuousAPI(BaseDataAPI):
    """期貨連續合約 API（一天一列、換月已接好）"""

    DEFAULT_DB_PATH: Path = Path(TW_FUTURES_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = FuturesContinuousDAO
    LOG_FILE_NAME: str = "futures_continuous_api.log"

    # 建構、連線與 log 由 `BaseDataAPI` 負責；本類只寫查詢

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
        # 不是查詢寫錯（全新環境本來就會走到這裡）；DAO 宣告為可缺的表，缺表回空表
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
            取連續合約的收盤價序列

            對標曲線只要收盤價，`get_series()` 的其餘欄位對它沒有意義。
        - Parameters:
            - 同 `get_series()`
        - Return:
            - pd.Series
                價格序列（契約見 `BaseDataAPI.build_close_series()`：index 為
                `datetime.date`、dtype float、name 為「收盤價」）；查無資料時為空 Series
        """

        df: pd.DataFrame = self.get_series(
            product,
            start_date,
            end_date,
            session=session,
            method=method,
            roll_rule=roll_rule,
        )
        return self.build_close_series(df, "date", FuturesPriceColumn.CLOSE.value)
