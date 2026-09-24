import datetime
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import numpy as np
import pandas as pd
from loguru import logger

from core.api.base import BaseDataAPI
from core.config import (
    CORPORATE_ACTION_TABLE_NAME,
    TW_STOCK_DB_PATH,
)
from core.dao.base import BaseDAO
from core.dao.connection import DBConnection
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO

"""
Stock dividend API: query dividend table through StockDividendDAO（除權除息計算結果表）

本表同時服務兩個互不相同的需求，取值時務必分清楚：
- **價格序列還原**：用「還原係數」
- **放空的股利補償現金流**：用「現金股利」

具名查詢方法一律回傳 `{stock_id: 值}` 對照表，與 `StockPriceAPI` 的
`get_close_map()` 系列同型，策略層與回測層不需要知道資料表欄位名。
"""


class StockDividendAPI(BaseDataAPI):
    """Stock dividend API"""

    DEFAULT_DB_PATH: Path = Path(TW_STOCK_DB_PATH)
    DAO_CLASS: Type[BaseDAO] = StockDividendDAO
    LOG_FILE_NAME: str = "stock_dividend_api.log"

    def __init__(self, conn: Optional[DBConnection] = None) -> None:
        # 後復權累乘係數快取：{stock_id: (除權息日 ndarray, 累乘係數 ndarray)}
        # 回測會逐日呼叫，每次重掃全表不划算，故整表只載入一次
        self.factor_cache: Optional[Dict[str, Tuple[np.ndarray, np.ndarray]]] = None

        super().__init__(conn)

    def setup(self) -> None:
        """建連線與主 DAO 由基底負責；此處只補本 API 多出來的 DAO"""

        super().setup()
        self.corporate_action_dao: CorporateActionDAO = CorporateActionDAO(
            conn=self.conn
        )

    def get(self, date: datetime.date) -> pd.DataFrame:
        """取得所有股票指定日期的除權除息資料"""

        return self.dao.get_by_date(date)

    def get_range(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得所有股票日期範圍內的除權除息資料"""

        return self.dao.get_range(start_date, end_date)

    def get_stock_dividend(
        self,
        stock_id: str,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> pd.DataFrame:
        """取得指定個股的除權除息資料（依日期排序，供還原係數累乘使用）"""

        return self.dao.get_by_stock(stock_id, start_date, end_date)

    def get_adjust_factor_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得指定日期除權息的還原係數對照表

            係數 = 除權息參考價 / 除權息前收盤價（恆 < 1），代表除權息造成的價格落差比例。
            **沒有除權息的股票不會出現在回傳值中**——key 不存在即代表當日無需還原，
            呼叫端不應以 1.0 當作預設值寫進資料，兩者語意不同

        - Parameters:
            - date: datetime.date
                除權息交易日

        - Return:
            - Dict[str, float]
                `{stock_id: 還原係數}`；當日無除權息時回傳空 dict
        """

        return self.build_column_map(self.get(date), "還原係數")

    def get_cash_dividend_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得指定日期的每股現金股利對照表（單位：元／股）

            供融券放空的股利補償計算使用；純除權（無現金股利）時值為 0

        - Parameters:
            - date: datetime.date
                除權息交易日

        - Return:
            - Dict[str, float]
                `{stock_id: 現金股利}`；當日無除權息時回傳空 dict
        """

        return self.build_column_map(self.get(date), "現金股利")

    def get_stock_dividend_ratio_map(self, date: datetime.date) -> Dict[str, float]:
        """取得指定日期的每股配股數對照表（純除息時為 0）"""

        return self.build_column_map(self.get(date), "配股率")

    def get_share_ratio_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得指定日期的**股數倍率**對照表（`新股數 / 舊股數`）

            兩個來源合併，回答的是同一個問題「手上的股數今天要乘以多少」：

            | 來源 | 欄位 | 換算 | 例 |
            |------|------|------|----|
            | `dividend` | `配股率`（每股配發股數） | `1 + 配股率` | 配股 0.05 → 1.05 |
            | `corporate_action` | `調整倍率`（價格倍率） | `1 / 調整倍率` | 分割價格砍半 0.5 → 2.0 |

            **與還原係數的分工**：還原係數只用於訊號面的價格序列；本方法用於
            記帳面——做多部位的股數與每股成本要跟著調整，否則張數不變、價格砍半，
            帳面會憑空虧一半。兩者都需要，少了任何一邊都會有一段假損益。

            同一天兩個來源都有時以 `dividend` 為準，與 `get_adjust_factors()`
            的取捨一致（交易所對除權息的參考價已含該日全部調整）。

            **配股率為 NULL 時回傳 NaN，不當成 0**：上市權息並存的列拆不出配股率，
            cleaner 刻意留 NULL。當成 0 就是「沒有配股」，做多部位持有過除權日時
            價格已除權、張數卻沒放大，帳面憑空虧掉配股那一段。NaN 交給結算層
            記 warning 並計數，與現金股利 NULL 的口徑一致。
        - Parameters:
            - date: datetime.date
                交易日（除權息交易日／恢復買賣日）
        - Return:
            - Dict[str, float]
                `{stock_id: 股數倍率}`；倍率為 1（無變動）者不列入，
                配股率未知者為 NaN
        """

        ratios: Dict[str, float] = {}

        action_df: pd.DataFrame = self.corporate_action_dao.get_adjust_ratios_by_date(
            date
        )
        for stock_id, ratio in self.build_column_map(action_df, "調整倍率").items():
            value: float = self.to_float(ratio)
            if value > 0 and value != 1.0:
                ratios[str(stock_id)] = 1 / value

        for stock_id, share_ratio in self.get_stock_dividend_ratio_map(date).items():
            if self.is_missing(share_ratio):
                ratios[str(stock_id)] = math.nan
                continue
            value = self.to_float(share_ratio)
            if value > 0:
                ratios[str(stock_id)] = 1 + value

        return ratios

    @staticmethod
    def is_missing(value: Any) -> bool:
        """資料表的 NULL（讀進來是 None 或 NaN）"""

        if value is None:
            return True
        try:
            return math.isnan(float(value))
        except (TypeError, ValueError):
            return False

    @staticmethod
    def to_float(value: Any) -> float:
        """把資料表原樣取出的數值轉為 float；無法轉換者視為 0（＝沒有調整）"""

        try:
            result: float = float(value)
        except (TypeError, ValueError):
            return 0.0

        return 0.0 if math.isnan(result) else result

    def get_opening_reference_price_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得指定日期的開盤競價基準對照表

            除權息日的漲跌停須以此為基準，沿用前一交易日收盤會讓整段區間偏移

        - Parameters:
            - date: datetime.date
                除權息交易日

        - Return:
            - Dict[str, float]
                `{stock_id: 開盤競價基準}`；當日無除權息時回傳空 dict
        """

        return self.build_column_map(self.get(date), "開盤競價基準")

    # === 後復權累乘係數：還原價由這一組提供 ===
    def load_factor_cache(self) -> Dict[str, Tuple[np.ndarray, np.ndarray]]:
        """
        - Description:
            載入並快取每檔股票的**後復權累乘係數**

            後復權的定義：以最早日為基準，歷史價格不變、除權息之後的價格往上還原。
            某日 t 的累乘係數為該檔所有「除權息日 ≤ t」的單次係數倒數之連乘：

                factor(stock, t) = Π (除權息前收盤價 / 除權息參考價)

            單次係數（`還原係數` 欄位）恆 < 1，故其倒數 > 1，累乘後越晚的價格被放大越多，
            這正是把「除權息造成的跳空」從報酬率裡扣掉的效果。

            **不用前復權**：前復權會讓同一個歷史日期的價格隨著每次新除權息而改變，
            LONG baseline 會在每次除權息後自動失效，回歸保護等於形同虛設。

            快取在**整個 process 生命週期內有效**：回測期間資料表不會變動；
            若在同一個 process 內更新了 `dividend` 表，須自行呼叫 `reset_factor_cache()`

        - Return:
            - Dict[str, Tuple[np.ndarray, np.ndarray]]
                `{stock_id: (除權息日, 累乘係數)}`，兩個陣列皆已依日期排序
        """

        if self.factor_cache is not None:
            return self.factor_cache

        df: pd.DataFrame = self._load_adjustment_events()

        cache: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
        if df.empty:
            self.factor_cache = cache
            return cache

        df["date"] = pd.to_datetime(df["date"])
        # 單次係數為 0 或負值代表清洗端漏擋，這裡再擋一次，避免整段累乘被汙染成 0／負數
        df = df[df["還原係數"] > 0]
        df["累乘係數"] = df.groupby("stock_id")["還原係數"].transform(
            lambda s: (1.0 / s).cumprod()
        )

        for stock_id, group in df.groupby("stock_id"):
            cache[str(stock_id)] = (
                group["date"].to_numpy(dtype="datetime64[ns]"),
                group["累乘係數"].to_numpy(dtype=float),
            )

        self.factor_cache = cache
        return cache

    def _load_adjustment_events(self) -> pd.DataFrame:
        """
        - Description:
            取出**所有**會造成價格跳空的事件，除權息與非除權息的公司行動都算

            兩張表的欄位語意**完全一致**，都是「參考價 ÷ 前收盤價」，
            所以可以直接疊在一起走同一套累乘：

            | 表 | 欄位 | 典型值 | 意義 |
            |----|------|:---:|------|
            | `dividend` | `還原係數` | < 1 | 除權息造成價格下跳 |
            | `corporate_action` | `調整倍率` | 減資 > 1、分割 < 1 | 公司行動造成價格跳動 |

            **`corporate_action` 的倍率可以大於 1，這正是它不能併進 `dividend`
            的原因**（那一欄的既有讀取端都假設恆 < 1），但在這裡不構成問題：
            累乘的是倒數，方向由數值自己決定——減資倍率 9.99 的倒數是 0.1，
            會把事件之後的價格往下調回來，正是要的效果。

            同一個 `(date, stock_id)` 兩表都有時**以 `dividend` 為準**：
            減資與除權息同日是可能的，但 `dividend.還原係數` 是交易所對除權息
            算出來的官方參考價，語意更精確。實測全期間只有極少數會撞。
        - Return:
            - pd.DataFrame
                欄位 `date`／`stock_id`／`還原係數`，已依 `(stock_id, date)` 排序
        """

        dividend_df: pd.DataFrame = self.dao.get_adjust_factors()

        # `corporate_action` 是選用表，尚未回補的環境可能沒有——
        # 查不到時只用除權息，減資與分割的假跳空不會被消除
        if not self.corporate_action_dao.table_exists():
            logger.warning(
                f"[dividend] 找不到 {CORPORATE_ACTION_TABLE_NAME}，還原價僅涵蓋除權息；"
                "減資與分割的假跳空不會被消除。"
                "請跑 `python -m tasks.update_db --target corporate_action`"
            )
            return dividend_df.sort_values(["stock_id", "date"], kind="stable")

        action_df: pd.DataFrame = self.corporate_action_dao.get_adjust_ratios().rename(
            columns={"調整倍率": "還原係數"}
        )

        merged: pd.DataFrame = pd.concat([dividend_df, action_df], ignore_index=True)
        before: int = len(merged)
        # `keep="first"` 讓 dividend 勝出（它被 concat 在前面）
        merged = merged.drop_duplicates(subset=["date", "stock_id"], keep="first")
        if len(merged) < before:
            logger.info(
                f"[dividend] 除權息與公司行動同日重疊 {before - len(merged)} 筆，"
                "以除權息為準"
            )

        return merged.sort_values(["stock_id", "date"], kind="stable")

    def reset_factor_cache(self) -> None:
        """清掉累乘係數快取（更新 `dividend` 或 `corporate_action` 表後需呼叫）"""

        self.factor_cache = None

    def get_cumulative_factor(self, stock_id: str, date: datetime.date) -> float:
        """
        - Description:
            取得指定個股在指定日期的後復權累乘係數

        - Parameters:
            - stock_id: str
                股票代號
            - date: datetime.date
                查詢日期

        - Return:
            - float
                累乘係數；該日之前無除權息時為 `1.0`（代表不需還原）
        """

        cache: Dict[str, Tuple[np.ndarray, np.ndarray]] = self.load_factor_cache()
        entry: Optional[Tuple[np.ndarray, np.ndarray]] = cache.get(str(stock_id))

        if entry is None:
            return 1.0

        ex_dates, factors = entry
        # 除權息日「當日」即已套用新價格，故用 right 邊界（date >= 除權息日 才算數）
        idx: int = int(np.searchsorted(ex_dates, np.datetime64(date), side="right"))

        if idx == 0:
            return 1.0
        return float(factors[idx - 1])

    def get_cumulative_factor_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得單日全市場的後復權累乘係數對照表

            與 `get_adjust_factor_map()` 的差異（兩者容易混淆）：
            - `get_adjust_factor_map()`：**當日單次**除權息的落差比例，只有當日除權息的股票才有值
            - 本方法：**歷史累乘**係數，所有曾經除權息過的股票都有值

            未曾除權息的股票不會出現在回傳值中，呼叫端取不到時視為 `1.0`

        - Parameters:
            - date: datetime.date
                查詢日期

        - Return:
            - Dict[str, float]
                `{stock_id: 累乘係數}`
        """

        cache: Dict[str, Tuple[np.ndarray, np.ndarray]] = self.load_factor_cache()
        target: np.datetime64 = np.datetime64(date)

        factor_map: Dict[str, float] = {}
        for stock_id, (ex_dates, factors) in cache.items():
            idx: int = int(np.searchsorted(ex_dates, target, side="right"))
            if idx:
                factor_map[stock_id] = float(factors[idx - 1])

        return factor_map

    def get_ex_dividend_dates(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """取得日期範圍內所有出現除權息的交易日（已排序、去重）"""

        return self.dao.get_ex_dividend_dates(start_date, end_date)
