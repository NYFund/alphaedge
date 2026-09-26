import datetime
from typing import Any, List, Optional, Tuple

import pandas as pd
from dateutil.rrule import DAILY, rrule

"""
時間換算工具：民國年、日期區間產生、型別正規化

- Features:
    1. 民國年與西元年互轉（財報與月營收的來源頁面一律用民國年）
    2. 日期／年／季／年期別區間的產生（ETL 逐期爬取用）
    3. `to_date()` 把來源給的各種型別（字串、`datetime`、`pd.Timestamp`）
       收斂成 `datetime.date`
- 使用場景:
    全庫共用的純換算，不碰 DB 也不碰網路。ETL 各層與回測都會用到，
    故擺在 `core/utils/` 而不是 pipeline 底下。
"""


class TimeUtils:
    """處理各式關於時間問題的工具"""

    ROC_EPOCH_YEAR: int = 1911  # 民國年與西元年換算：西元 = 民國 + ROC_EPOCH_YEAR

    @staticmethod
    def convert_ad_to_roc_year(year: int | str) -> str:
        """將西元年轉換成民國年"""

        try:
            year_int: int = int(year)
            if year_int < TimeUtils.ROC_EPOCH_YEAR + 1:
                raise ValueError(
                    f"民國元年從 {TimeUtils.ROC_EPOCH_YEAR + 1} 年開始，請輸入有效的西元年份"
                )
            return str(year_int - TimeUtils.ROC_EPOCH_YEAR)
        except (ValueError, TypeError):
            raise ValueError(f"無效的年份輸入：{year}")

    @staticmethod
    def convert_roc_to_ad_year(year: int | str) -> int:
        """將民國年轉為西元年"""

        try:
            return int(year) + TimeUtils.ROC_EPOCH_YEAR
        except (ValueError, TypeError):
            raise ValueError(f"無效的年份輸入：{year}")

    @staticmethod
    def generate_date_range(
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> List[datetime.date]:
        """產生從 start_date 到 end_date 的每日日期清單"""

        return [dt.date() for dt in rrule(DAILY, dtstart=start_date, until=end_date)]

    @staticmethod
    def generate_year_range(
        start_year: int,
        end_year: int,
    ) -> List[int]:
        """產生從 start_year 到 end_year 的所有年份"""

        return [year for year in range(start_year, end_year + 1)]

    @staticmethod
    def generate_year_period_range(
        start_year: int,
        start_period: int,
        end_year: int,
        end_period: int,
        periods_per_year: int,
    ) -> List[Tuple[int, int]]:
        """
        - Description:
            產生連續的「(年, 期)」序列，期可以是季（4）或月（12）

            **不可用 `for year in years: for period in periods:` 的笛卡兒積**：
            起點 2024Q3、終點 2026Q4 時，`periods` 會是 `[3, 4]`，
            於是 2025Q1／Q2 與 2026Q1／Q2 **整整四季不會被爬**，而且不會有任何錯誤
            ——它們只是從來沒出現在迴圈裡。
        - Parameters:
            - start_year: int
                起始年
            - start_period: int
                起始期（1 起算）
            - end_year: int
                結束年
            - end_period: int
                結束期
            - periods_per_year: int
                一年幾期；季報為 4、月營收為 12
        - Return:
            - List[Tuple[int, int]]
                由早到晚的 (年, 期)；起點晚於終點時為空清單
        """

        if not 1 <= start_period <= periods_per_year:
            raise ValueError(f"start_period 應在 1 到 {periods_per_year} 之間")
        if not 1 <= end_period <= periods_per_year:
            raise ValueError(f"end_period 應在 1 到 {periods_per_year} 之間")

        start_index: int = start_year * periods_per_year + (start_period - 1)
        end_index: int = end_year * periods_per_year + (end_period - 1)

        return [
            (index // periods_per_year, index % periods_per_year + 1)
            for index in range(start_index, end_index + 1)
        ]

    @staticmethod
    def to_date(value: Any) -> Optional[datetime.date]:
        """
        - Description:
            把各處來源的日期欄位統一轉為 `datetime.date`

            報價的 `date` 可能是 `date`（日 K）或 `datetime`／`Timestamp`（tick），
            資料表讀出來則可能是字串。需要以日期做判斷的地方（例如依年代選取
            漲跌停幅度）不應各自處理這些型別差異。
        - Parameters:
            - value: Any
                日期值；可為 `date`、`datetime`、字串或 `pd.Timestamp`
        - Return:
            - Optional[datetime.date]
                轉換後的日期；無法解析時為 None
        """

        if isinstance(value, datetime.datetime):
            return value.date()
        if isinstance(value, datetime.date):
            return value
        if value is None:
            return None

        try:
            return pd.to_datetime(value).date()
        except (TypeError, ValueError):
            return None

    @staticmethod
    def format_date(date: datetime.date, sep: str = "") -> str:
        """Format date as 'YYYY{sep}MM{sep}DD'"""

        return date.strftime(f"%Y{sep}%m{sep}%d")
