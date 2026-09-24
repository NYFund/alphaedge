from abc import ABC, abstractmethod

import pandas as pd

from core.pipeline.utils.exceptions import ColumnLayoutError

"""
所有 cleaner 的共同基底

`check_column_count()` 是給**依位置命名欄位**的來源用的（上櫃的多張表都不給欄名）：
版面一改，位置命名會把每一欄都對到錯的名字——最低價變成成交量、成交金額變成收盤價
——而且完全不會報錯，資料照樣入庫。與其事後從數字裡看出不對勁，
不如在命名之前先數一次欄位。
"""


class BaseDataCleaner(ABC):
    """Base Class of Data Cleaner"""

    def __init__(self) -> None:
        """建立 cleaner；設定一律由子類的 `setup()` 負責"""
        pass

    @abstractmethod
    def setup(self, *args, **kwargs) -> None:
        """Set Up the Config of Cleaner"""
        pass

    @staticmethod
    def check_column_count(df: pd.DataFrame, expected: int, label: str) -> None:
        """
        - Description:
            依位置命名欄位前，先確認欄位數與預期相符
        - Parameters:
            - df: pd.DataFrame
                原始表格
            - expected: int
                預期的欄位數
            - label: str
                來源與日期的描述，只用於錯誤訊息
        - Raise:
            - ColumnLayoutError
                欄位數不符
        """

        actual: int = len(df.columns)
        if actual != expected:
            raise ColumnLayoutError(label, expected, actual, list(df.columns))
