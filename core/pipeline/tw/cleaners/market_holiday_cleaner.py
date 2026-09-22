from typing import Tuple

import pandas as pd

from core.pipeline.shared.base_cleaner import BaseDataCleaner

"""
市場開休市日期清洗：把「提醒列」與「休市列」分開

TWSE 的開休市表不只列休市日，還夾著幾種**當天其實有開市**的提醒列：

| 名稱（例） | 當天 | 為什麼會出現在表上 |
|------------|------|--------------------|
| 國曆新年開始交易日、農曆春節後開始交易日 | 開市 | 提醒連假後第一個交易日 |
| 農曆春節前最後交易日 | 開市 | 提醒連假前最後一個交易日 |
| 補行交易日（早年的週六補班） | 開市 | 平常不開市的日子這天要開 |
| 市場無交易，僅辦理結算交割作業 | **休市** | 只做交割，不能下單 |

**只有名稱含上表前三種字樣的列才算交易日**，其餘一律當休市。反過來寫（列舉休市
字樣）的話，站方每新增一種假日名稱就會被當成交易日——那是在休市日照常送單。
週末的休市列（例如和平紀念日落在週六）照樣保留：與週末本來就休市一致，無害。
"""


class MarketHolidayCleaner(BaseDataCleaner):
    """市場開休市日期清洗"""

    # 名稱含這些字樣的列是「當天有開市」的提醒，不是休市
    TRADING_DAY_MARKERS: Tuple[str, ...] = ("開始交易", "最後交易", "補行交易")

    # 站方回傳的欄位數（日期、名稱、說明）
    RAW_COLUMN_COUNT: int = 3

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""
        pass

    @classmethod
    def is_trading_day_name(cls, name: str) -> bool:
        """名稱是否為「當天有開市」的提醒列"""

        return any(marker in name for marker in cls.TRADING_DAY_MARKERS)

    def clean(self, df: pd.DataFrame, year: int) -> pd.DataFrame:
        """
        - Description:
            清洗單一年度的開休市表

            **日期年份必須等於查詢年度**：站方若回了別的年度（參數被忽略、
            退回預設年），整年寫進錯的年份會讓那一年看起來「已涵蓋」卻全是錯的。
        - Parameters:
            - df: pd.DataFrame
                crawler 回傳的原始表（`日期`／`名稱`／`說明`）
            - year: int
                查詢年度
        - Return:
            - pd.DataFrame
                `date`／`name`／`description`／`is_trading_day`／`year`
        - Raise:
            - ColumnLayoutError
                欄位數不符
            - ValueError
                日期解析失敗，或年份與查詢年度不符
        """

        self.check_column_count(
            df, self.RAW_COLUMN_COUNT, f"TWSE holiday schedule {year}"
        )

        cleaned: pd.DataFrame = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    df.iloc[:, 0].astype(str).str.strip(), format="%Y-%m-%d"
                ),
                "name": df.iloc[:, 1].fillna("").astype(str).str.strip(),
                # 說明欄常夾著 `\r\n`（站方原文的換行），入庫前去掉
                "description": df.iloc[:, 2]
                .fillna("")
                .astype(str)
                .str.replace(r"\s+", " ", regex=True)
                .str.strip(),
            }
        )

        wrong_year: pd.Series = cleaned["date"].dt.year != year
        if wrong_year.any():
            raise ValueError(
                f"TWSE holiday schedule {year} 含其他年度的日期："
                f"{cleaned.loc[wrong_year, 'date'].dt.date.tolist()[:5]}"
            )

        cleaned["is_trading_day"] = (
            cleaned["name"].map(self.is_trading_day_name).astype(int)
        )
        cleaned["year"] = year
        cleaned["date"] = cleaned["date"].dt.strftime("%Y-%m-%d")

        # 同一天若同時有休市列與提醒列，以休市為準：誤判成休市頂多少跑一天，
        # 誤判成開市則是在休市日送單
        cleaned = cleaned.sort_values(["date", "is_trading_day"]).drop_duplicates(
            subset="date", keep="first"
        )

        return cleaned.reset_index(drop=True)
