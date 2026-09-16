import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.api.tw.futures_price_api import FuturesPriceAPI
from core.backtest.datafeed.tw.futures_calendar import FuturesCalendar
from core.backtest.datafeed.tw.futures_roll import FuturesRollPlanner
from core.config import (
    DEFAULT_FUTURES_START_DATE,
    FUTURES_TARGET_PRODUCTS,
    TW_FUTURES_DB_PATH,
)
from core.config.schema import FuturesPriceColumn
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.loaders.futures_continuous_loader import FuturesContinuousLoader
from core.pipeline.utils.exceptions import DataLoadError, UnbuildableSeriesError
from core.utils import FuturesAdjustMethod, FuturesRollRule, FuturesSession
from core.utils.log_manager import LogManager

"""
Futures Continuous Updater: 由各月份契約建出可跨月的連續序列

**這一組沒有 crawler 也沒有 cleaner**：來源是同一個 DB 裡的
`futures_price_daily`，不是網路。四層架構在此退化為「建表 ＋ 入庫」兩層，
硬湊一個空的 crawler 只會讓人以為它有去抓什麼。

---

**為什麼需要連續合約**：單一契約的壽命只有幾個月，任何需要長序列的計算
（移動平均、波動度、年線）在契約到期那天都會斷掉。把各月份接起來會在接點
產生**展期價差**造成的假跳空——2024-03-20 換月當天 202403 收 19,981、
202404 收 19,919，差 62 點，不調整就會被指標當成真實的跳空。

**三種調整方式的差別見 `FuturesAdjustMethod`**，本 updater 三種都建，
存在同一張表的不同 `method`：它們回答的問題不同，沒有一種可以取代另一種。

**逆向調整是「往回減」不是「往前加」**：以最新一段為基準，把更早的價格各自
減去其後所有展期價差的總和。因此**最新那一段的價格等於真實成交價**，
而歷史價格會與當時的成交價不同——這是刻意的，也是業界（Panama 調整）的慣例。
`adj_factor` 欄存下已套用的調整量，`原始價 ＝ 調整價 ＋ adj_factor`
（RATIO 則為 `原始價 ＝ 調整價 × adj_factor`），隨時可還原檢查。
"""


class FuturesContinuousUpdater(BaseDataUpdater):
    """建立並更新 `futures_continuous`"""

    # 建表時一併產生的組合：三種調整方式 × 指定的換月規則
    DEFAULT_METHODS: List[FuturesAdjustMethod] = [
        FuturesAdjustMethod.NONE,
        FuturesAdjustMethod.BACKWARD,
        FuturesAdjustMethod.RATIO,
    ]

    # 換月規則預設只建「撐到最後交易日」這一種——另外兩種由呼叫端指定。
    # **不預設全建**：`OPEN_INTEREST` 需要逐日的未沖銷契約量，成本高於前兩者
    DEFAULT_ROLL_RULES: List[FuturesRollRule] = [FuturesRollRule.LAST_TRADING_DAY]

    def __init__(self) -> None:
        super().__init__()

        self.price_api: Optional[FuturesPriceAPI] = None
        self.loader: Optional[FuturesContinuousLoader] = None
        # 行情讀取與序列寫入同在 tw_futures.db，共用同一個 DAO 的連線，由本 updater 關閉
        self.dao: Optional[FuturesContinuousDAO] = None

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Updater"""

        LogManager.setup_logger("futures_continuous_updater.log")
        TW_FUTURES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.dao = FuturesContinuousDAO(db_path=TW_FUTURES_DB_PATH)
        self.price_api = FuturesPriceAPI(conn=self.dao.conn)
        self.loader = FuturesContinuousLoader(dao=self.dao)

    def close(self) -> None:
        """關閉資料連線（行情 API 與 loader 共用同一條，一併結束）"""

        if self.dao is not None:
            self.dao.close()
            self.dao = None

    def update(
        self,
        products: Optional[List[str]] = None,
        start_date: Optional[datetime.date] = None,
        end_date: Optional[datetime.date] = None,
        session: FuturesSession = FuturesSession.DAY,
        methods: Optional[List[FuturesAdjustMethod]] = None,
        roll_rules: Optional[List[FuturesRollRule]] = None,
        days_before_expiry: int = FuturesRollPlanner.DEFAULT_DAYS_BEFORE_EXPIRY,
    ) -> None:
        """
        - Description:
            重建指定商品的連續合約序列

            **每次都是整段重建，不做增量**：連續合約是衍生結果，逆向調整的
            調整量會隨「之後又發生了幾次換月」而改變——今天新增一次換月，
            所有歷史列的 `adj_factor` 都會跟著變。增量更新在這裡不但沒有好處，
            還會讓表裡混著兩代基準的價格。全量重建 TX 12 年也只要幾秒。
        - Parameters:
            - products: Optional[List[str]]
                商品代碼；None 取 `FUTURES_TARGET_PRODUCTS`
            - start_date / end_date: Optional[datetime.date]
                建表區間；None 取 `DEFAULT_FUTURES_START_DATE` ~ 今天
            - session: FuturesSession
                交易時段（連續合約一律單一時段，日夜盤整併在回測報價層處理）
            - methods / roll_rules: Optional[List[...]]
                要建的調整方式與換月規則組合
            - days_before_expiry: int
                `DAYS_BEFORE_EXPIRY` 規則的提前天數
        """

        targets: List[str] = products or list(FUTURES_TARGET_PRODUCTS)
        start: datetime.date = start_date or DEFAULT_FUTURES_START_DATE
        end: datetime.date = end_date or datetime.date.today()

        logger.info(f"* Start building futures continuous series: {targets}")

        failures: List[str] = []

        for product in targets:
            for roll_rule in roll_rules or self.DEFAULT_ROLL_RULES:
                try:
                    self.update_product(
                        product=product,
                        start_date=start,
                        end_date=end,
                        session=session,
                        methods=methods or self.DEFAULT_METHODS,
                        roll_rule=roll_rule,
                        days_before_expiry=days_before_expiry,
                    )
                except UnbuildableSeriesError as error:
                    # 單一商品排不出換月表不該中止其餘商品，但跑完必須拋出——
                    # 只記 warning 的話，連續合約整段沒重建也是結束碼 0
                    logger.error(f"[Futures Continuous] {error}")
                    failures.append(f"{product}/{roll_rule.value}")

        if failures:
            raise DataLoadError(
                "futures_continuous",
                failures,
                succeeded=len(targets) * len(roll_rules or self.DEFAULT_ROLL_RULES)
                - len(failures),
            )

    def update_product(
        self,
        product: str,
        start_date: datetime.date,
        end_date: datetime.date,
        session: FuturesSession,
        methods: List[FuturesAdjustMethod],
        roll_rule: FuturesRollRule,
        days_before_expiry: int,
    ) -> int:
        """建立單一商品、單一換月規則之下的所有調整方式；回傳寫入列數"""

        price_df: pd.DataFrame = self.price_api.get_range(
            start_date, end_date, product=product, session=session
        )
        if price_df.empty:
            # **這是正常狀態**：該商品可能還沒回補、或在區間內尚未上市，
            # 與「有行情卻排不出換月表」是兩回事，故不列為失敗
            logger.warning(f"[Futures Continuous] {product} 在區間內沒有行情，跳過")
            return 0

        calendar: FuturesCalendar = FuturesCalendar.from_api(
            self.price_api, start_date, end_date, product=product
        )
        planner: FuturesRollPlanner = FuturesRollPlanner(
            calendar, rule=roll_rule, days_before_expiry=days_before_expiry
        )

        schedule: Dict[datetime.date, str] = planner.build_roll_schedule(
            dates=sorted({self.to_date(value) for value in price_df["date"]}),
            expiries_by_date=self.build_expiries_by_date(price_df),
            open_interest_by_date=self.build_open_interest_by_date(price_df),
        )
        if not schedule:
            # 有行情卻排不出換月表＝到期月代碼異常或日曆有問題，是真的出錯
            raise UnbuildableSeriesError(
                f"{product} / {roll_rule.value} 有 {len(price_df)} 列行情卻排不出換月表"
            )

        series: pd.DataFrame = self.build_series(price_df, schedule)

        # 同一組（商品, 換月規則）的各種調整方式一起落地：中途失敗時整組回滾，
        # 表裡不會只有 BACKWARD 是新的、RATIO 還是上一代，也不會被下一個商品的 commit 帶進去
        total: int = 0
        with self.loader.dao.savepoint("futures_continuous_series"):
            for method in methods:
                adjusted: pd.DataFrame = self.apply_adjustment(series.copy(), method)
                adjusted.insert(1, "product", product)
                adjusted.insert(2, "session", session.value)
                adjusted.insert(3, "method", method.value)
                adjusted.insert(4, "roll_rule", roll_rule.value)

                total += self.loader.add_to_db(adjusted)
                self.loader.save_csv(
                    adjusted,
                    f"{product}_{session.value}_{method.value}_{roll_rule.value}.csv",
                )
        self.loader.commit()

        logger.info(
            f"[Futures Continuous] {product} / {roll_rule.value}："
            f"{len(series)} 個交易日、{int(series['roll_flag'].sum())} 次換月、"
            f"寫入 {total} 列"
        )
        return total

    # === 資料整理 ===
    @staticmethod
    def to_date(value: Any) -> datetime.date:
        """行情表的 `date` 欄是字串，統一轉成 `datetime.date`"""

        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        return datetime.date.fromisoformat(str(value)[:10])

    def build_expiries_by_date(
        self, price_df: pd.DataFrame
    ) -> Dict[datetime.date, List[str]]:
        """`{交易日: [當日掛牌中的到期月]}`"""

        expiries_by_date: Dict[datetime.date, List[str]] = {}
        for date_value, expiry in zip(price_df["date"], price_df["expiry"]):
            expiries_by_date.setdefault(self.to_date(date_value), []).append(
                str(expiry)
            )
        return expiries_by_date

    def build_open_interest_by_date(
        self, price_df: pd.DataFrame
    ) -> Dict[datetime.date, Dict[str, Any]]:
        """`{交易日: {到期月: 未沖銷契約量}}`；`OPEN_INTEREST` 換月規則需要"""

        column: str = FuturesPriceColumn.OPEN_INTEREST.value
        if column not in price_df.columns:
            return {}

        open_interest: Dict[datetime.date, Dict[str, Any]] = {}
        for date_value, expiry, value in zip(
            price_df["date"], price_df["expiry"], price_df[column]
        ):
            open_interest.setdefault(self.to_date(date_value), {})[str(expiry)] = value
        return open_interest

    def build_series(
        self, price_df: pd.DataFrame, schedule: Dict[datetime.date, str]
    ) -> pd.DataFrame:
        """
        - Description:
            依換月表取出每日的當家契約，並算出換月接點的展期價差

            **展期價差取「同一天、兩個契約的收盤價差」**（新 − 舊）：那是轉倉當下
            實際要付出的價差。換月當天若舊契約已無報價（例如結算後才換），
            則往前找最近一個兩者都有報價的交易日；真的找不到就記 0 並警告——
            **不可猜**，猜出來的價差會被當成真實的調整量寫進歷史每一列。
        - Parameters:
            - price_df: pd.DataFrame
                區間內的所有契約行情
            - schedule: Dict[datetime.date, str]
                `{交易日: 當家契約}`
        - Return:
            - pd.DataFrame
                每日一列的序列（含 `roll_flag` 與 `roll_gap`）
        """

        close_column: str = FuturesPriceColumn.CLOSE.value
        by_key: Dict[tuple, Dict[str, Any]] = {
            (self.to_date(row["date"]), str(row["expiry"])): row
            for _, row in price_df.iterrows()
        }

        rows: List[Dict[str, Any]] = []
        previous_expiry: Optional[str] = None

        for date in sorted(schedule):
            expiry: str = schedule[date]
            row: Optional[Dict[str, Any]] = by_key.get((date, expiry))
            if row is None:
                continue

            roll_flag: int = 1 if previous_expiry and expiry != previous_expiry else 0
            roll_gap: float = 0.0
            roll_ratio: float = 1.0
            if roll_flag:
                roll_gap, roll_ratio = self.calculate_roll_spread(
                    by_key, date, previous_expiry, expiry, close_column
                )

            rows.append(
                {
                    "date": str(date),
                    "expiry": expiry,
                    FuturesPriceColumn.OPEN.value: row[FuturesPriceColumn.OPEN.value],
                    FuturesPriceColumn.HIGH.value: row[FuturesPriceColumn.HIGH.value],
                    FuturesPriceColumn.LOW.value: row[FuturesPriceColumn.LOW.value],
                    close_column: row[close_column],
                    FuturesPriceColumn.VOLUME.value: row[
                        FuturesPriceColumn.VOLUME.value
                    ],
                    FuturesPriceColumn.SETTLEMENT.value: row[
                        FuturesPriceColumn.SETTLEMENT.value
                    ],
                    FuturesPriceColumn.OPEN_INTEREST.value: row[
                        FuturesPriceColumn.OPEN_INTEREST.value
                    ],
                    "roll_flag": roll_flag,
                    "roll_gap": roll_gap,
                    # 比例調整用；不入庫（見 `apply_adjustment()`）
                    "roll_ratio": roll_ratio,
                }
            )
            previous_expiry = expiry

        return pd.DataFrame(rows)

    def calculate_roll_spread(
        self,
        by_key: Dict[tuple, Dict[str, Any]],
        date: datetime.date,
        old_expiry: str,
        new_expiry: str,
        close_column: str,
    ) -> tuple:
        """
        - Description:
            換月的展期價差與展期比例，**兩者取自同一天的兩個契約**

            差額調整用價差（新 − 舊）、比例調整用比值（新 ÷ 舊），
            但兩者必須來自同一組報價，否則兩種方式會在同一個接點對不上。

            換月當日舊契約通常已無報價（`LAST_TRADING_DAY` 規則是結算後才換），
            故往前找最近一個兩者都有報價的交易日。真的找不到就回
            `(0.0, 1.0)` 並警告——**不可猜**，猜出來的價差會被寫進歷史每一列。
        - Parameters:
            - by_key: Dict[tuple, Dict[str, Any]]
                `{(日期, 到期月): 行情列}`
            - date: datetime.date
                換月日（序列第一天採用新契約的那天）
            - old_expiry / new_expiry: str
                舊契約與新契約
            - close_column: str
                收盤價欄名
        - Return:
            - tuple
                （展期價差, 展期比例）
        """

        for offset in range(0, 8):
            probe: datetime.date = date - datetime.timedelta(days=offset)
            old_row: Optional[Dict[str, Any]] = by_key.get((probe, old_expiry))
            new_row: Optional[Dict[str, Any]] = by_key.get((probe, new_expiry))
            if old_row is None or new_row is None:
                continue

            old_close: float = float(old_row[close_column])
            new_close: float = float(new_row[close_column])
            ratio: float = new_close / old_close if old_close else 1.0
            return (new_close - old_close, ratio)

        logger.warning(
            f"[Futures Continuous] {date} 由 {old_expiry} 換至 {new_expiry}，"
            f"但找不到兩者都有報價的日子，展期價差以 0 計"
        )
        return (0.0, 1.0)

    # === 價格調整 ===
    def apply_adjustment(
        self, series: pd.DataFrame, method: FuturesAdjustMethod
    ) -> pd.DataFrame:
        """
        - Description:
            套用調整方式，並填上可還原的 `adj_factor`

            **逆向調整（Panama）的方向是「把舊的往新的對齊」**：以最新一段為基準，
            每一列**加上**其後所有換月的展期價差總和。最新一段的 `adj_factor`
            因此為 0（BACKWARD）或 1（RATIO），價格完全等於真實成交價；
            越早的資料被調整得越多。

            **方向搞反是靜默的**：把加號寫成減號同樣能產出一條連續的序列、
            同樣能通過還原檢查，只是換月接點的日變動會變成「真實變動 ＋ 兩倍價差」。
            唯一抓得到的檢查是「調整後的換月日變動 ＝ 新契約自己的日變動」，
            見 `tests/test_futures_continuous.py`（本方向錯誤即由該測試抓出）。

            | 方式 | 套用 | 還原 |
            |------|------|------|
            | `BACKWARD` | 調整價 ＝ 原始價 ＋ `adj_factor` | 原始價 ＝ 調整價 − `adj_factor` |
            | `RATIO` | 調整價 ＝ 原始價 × `adj_factor` | 原始價 ＝ 調整價 ÷ `adj_factor` |
            | `NONE` | 不調整（`adj_factor` 恆為 0） | 原始價 ＝ 調整價 |
        - Parameters:
            - series: pd.DataFrame
                `build_series()` 的輸出（含內部欄位 `roll_ratio`）
            - method: FuturesAdjustMethod
                調整方式
        - Return:
            - pd.DataFrame
                含調整後價格與 `adj_factor` 的序列；內部欄位已移除
        """

        price_columns: List[str] = [
            FuturesPriceColumn.OPEN.value,
            FuturesPriceColumn.HIGH.value,
            FuturesPriceColumn.LOW.value,
            FuturesPriceColumn.CLOSE.value,
            FuturesPriceColumn.SETTLEMENT.value,
        ]

        if method == FuturesAdjustMethod.NONE:
            series["adj_factor"] = 0.0
            return series.drop(columns=["roll_ratio"])

        # 每一列「之後」發生的換月：由後往前累加（價差）／累乘（比例）
        pending_offset: float = 0.0
        pending_ratio: float = 1.0
        offsets: List[float] = []
        ratios: List[float] = []

        for index in range(len(series) - 1, -1, -1):
            offsets.append(pending_offset)
            ratios.append(pending_ratio)

            if series.iloc[index]["roll_flag"] == 1:
                pending_offset += float(series.iloc[index]["roll_gap"])
                pending_ratio *= float(series.iloc[index]["roll_ratio"])

        offsets.reverse()
        ratios.reverse()

        series = series.drop(columns=["roll_ratio"])

        if method == FuturesAdjustMethod.BACKWARD:
            series["adj_factor"] = offsets
            for column in price_columns:
                series[column] = series[column] + series["adj_factor"]
            return series

        series["adj_factor"] = ratios
        for column in price_columns:
            series[column] = series[column] * series["adj_factor"]
        return series
