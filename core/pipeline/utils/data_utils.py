import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd
from loguru import logger

from core.utils import FileEncoding

"""
Pipeline 共用的資料處理工具

清洗階段跨來源共用的小工具：DataFrame 欄位搬移／刪除／轉型、欄位名稱標準化
（各來源的全半形括號、破折號、空白寫法都不一致），以及 JSON 中繼檔的讀寫。
不綁任何市場，只有跨市場都適用的操作才放這裡。
"""

# 全專案 JSON 儲存預設縮排
DEFAULT_JSON_INDENT: int = 2


class DataUtils:
    """資料處理工具（欄位整理、名稱標準化、JSON 讀寫）"""

    @staticmethod
    def move_col(
        df: pd.DataFrame,
        col_name: str,
        ref_col_name: str,
    ) -> None:
        """移動 columns 位置：將 col_name 整個 column 移到 ref_col_name 後方"""

        col_data: pd.Series = df.pop(col_name)
        df.insert(df.columns.get_loc(ref_col_name) + 1, col_name, col_data)

    @staticmethod
    def remove_last_n_rows(df: pd.DataFrame, n_rows: int = 1) -> pd.DataFrame:
        """刪除 DataFrame 中最後 n row"""

        if len(df) <= n_rows:
            return df.iloc[0:0]  # 列數不足時回傳空表（保留欄位結構）
        return df.iloc[:-n_rows]

    @staticmethod
    def convert_col_to_numeric(
        df: pd.DataFrame, exclude_cols: List[str]
    ) -> pd.DataFrame:
        """將 exclude_cols 以外的 columns 資料都轉為數字型態（int or float）"""

        for col in df.columns:
            if col not in exclude_cols:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    @staticmethod
    def fill_nan(
        df: pd.DataFrame,
        value: int = 0,
        exclude_cols: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """
        - Description:
            把 NaN 填補為指定值，`exclude_cols` 的欄位維持 NaN

            **價格欄一定要排除**：無成交日的 OHLC 在來源是 `--`，轉數值後為 NaN，
            填成 0 之後就變成「當天成交價是 0 元」——那是一個**看起來完全正常的
            假價格**，回測會照著它成交（實測 `price` 表有 104,046 列無成交日）。
            成交量、成交金額、成交筆數填 0 則是正確的。
        - Parameters:
            - df: pd.DataFrame
                要處理的資料
            - value: int
                填補值
            - exclude_cols: Optional[List[str]]
                不填補、維持 NaN 的欄位（入庫後即為 NULL）
        - Return:
            - pd.DataFrame
        """

        if not df.isnull().values.any():
            return df

        keep_cols: List[str] = [
            col for col in (exclude_cols or []) if col in df.columns
        ]
        if not keep_cols:
            df.fillna(value, inplace=True)
            return df

        target_cols: List[str] = [col for col in df.columns if col not in keep_cols]
        df[target_cols] = df[target_cols].fillna(value)
        return df

    @staticmethod
    def check_required_columns(
        df: pd.DataFrame,
        required_cols: List[str],
        required_all: bool = True,
    ) -> bool:
        """
        - Description:
            檢查 DataFrame 是否包含必要欄位；清洗前驗證來源版面用
        - Parameters:
            - df: pd.DataFrame
                欲檢查的 DataFrame
            - required_cols: List[str]
                欲確認是否存在的欄位名稱列表
            - required_all: bool
                True（預設）表示所有欄位皆需存在；False 表示存在任一欄位即可
        - Return:
            - bool
                是否符合條件
        """

        if required_all:
            return all(col in df.columns for col in required_cols)
        else:
            return any(col in df.columns for col in required_cols)

    @staticmethod
    def standardize_column_name(
        word: str,
        replace_pairs: Dict[str, str] = {
            "（": "(",
            "）": ")",
            "：": ":",
        },
        remove_chars: List[str] = [],
        remove_dash: List[str] = [
            "－",
            "-",
            "—",
            "–",
            "─",
        ],
        remove_whitespace: bool = True,
    ) -> str:
        """
        - Description:
            清除空白與特殊符號（括號、全半形減號），標準化欄位名稱用

            同一個欄位在各來源與各年度的寫法會有全半形、破折號與空白的差異，
            不先拉齊就會被當成兩個不同的欄位。
        - Parameters:
            - word: str
                欲清理的欄位名稱
            - replace_pairs: Dict[str, str]
                要替換的字元對（Ex: {"（": "(", "）": ")"}）
            - remove_chars: List[str]
                要直接刪除的字元
            - remove_dash: List[str]
                要刪除的 dash（全形與半形共五種寫法）
            - remove_whitespace: bool
                是否移除所有空白（含 tab、換行、全形空白）
        - Return:
            - str
                處理後的欄位名稱
        """

        word: str = str(word)

        if remove_whitespace:
            word: str = re.sub(
                r"\s+", "", word
            )  # 清除所有空白（包含 tab, 換行, 全形空白）

        for old, new in replace_pairs.items():
            word: str = word.replace(old, new)
        for char in remove_chars:
            word: str = word.replace(char, "")
        for dash in remove_dash:
            word: str = word.replace(dash, "")

        return word

    @staticmethod
    def map_column_name(col: str, column_map: Dict[str, List[str]]) -> str:
        """將欄位名稱對應至標準名稱，若無對應則回傳原名"""

        for std_col, variants in column_map.items():
            if col in variants:
                return std_col
        return col

    @staticmethod
    def remove_cols_by_keywords(
        df: pd.DataFrame,
        startswith: Optional[List[str]] = None,
        contains: Optional[List[str]] = None,
        case_insensitive: bool = True,
    ) -> pd.DataFrame:
        """
        - Description:
            移除以指定字串開頭或包含指定字串的欄位
        - Parameters:
            - df: pd.DataFrame
                原始 DataFrame
            - startswith: Optional[List[str]]
                欲刪除欄位的開頭關鍵字（Ex: ["Unnamed"]）
            - contains: Optional[List[str]]
                欲刪除欄位的包含關鍵字（Ex: ["錯誤"]）
            - case_insensitive: bool
                是否忽略大小寫（預設 True）
        - Return:
            - pd.DataFrame
                已刪除指定欄位的 DataFrame（欄名維持原本大小寫）
        """

        # 確保 startswith / contains 一定是 list 型別，避免為 None 時無法迭代
        startswith_list: List[str] = startswith or []
        contains_list: List[str] = contains or []

        # 將欄位轉成 str 型別，並保留原始 index
        columns: pd.Index = df.columns.astype(str)

        # 初始化刪除遮罩（與欄位 index 對齊）
        columns_to_drop: pd.Series = pd.Series(False, index=columns)

        if case_insensitive:
            columns = columns.str.lower()
            startswith_list: List[str] = [word.lower() for word in startswith_list]
            contains_list: List[str] = [word.lower() for word in contains_list]

        for keyword in startswith_list:
            columns_to_drop |= columns.str.startswith(keyword)

        for keyword in contains_list:
            columns_to_drop |= columns.str.contains(keyword)

        return df.loc[:, ~columns_to_drop]

    @staticmethod
    def remove_items_by_keywords(
        items: List[str],
        startswith: Optional[List[str]] = None,
        contains: Optional[List[str]] = None,
        case_insensitive: bool = True,
    ) -> List[str]:
        """
        - Description:
            移除符合指定關鍵字的欄位名稱（以開頭或包含），並回傳保留的欄位清單
        - Parameters:
            - items: List[str]
                欲過濾的欄位名稱清單
            - startswith: Optional[List[str]]
                欲排除的開頭字串（Ex: ["Unnamed"]）
            - contains: Optional[List[str]]
                欲排除的部分字串（Ex: ["錯誤"]）
            - case_insensitive: bool
                是否忽略大小寫（預設 True）
        - Return:
            - List[str]
                過濾後保留的欄位名稱；`case_insensitive` 為 True 時一併轉為小寫
        """

        startswith_list: List[str] = startswith or []
        contains_list: List[str] = contains or []
        items_list: List[str] = [str(item) for item in items]

        def normalize(s: str) -> str:
            return s.lower() if case_insensitive else s

        norm_starts: List[str] = [normalize(s) for s in startswith_list]
        norm_contains: List[str] = [normalize(s) for s in contains_list]

        new_items: List[str] = []

        for item in items_list:
            norm_item: str = normalize(item)
            if any(norm_item.startswith(s) for s in norm_starts):
                continue
            if any(c in norm_item for c in norm_contains):
                continue
            new_items.append(norm_item)

        return new_items

    @staticmethod
    def remove_duplicate_rows(
        df: pd.DataFrame,
        subset: List[str],
        keep: Union[str, bool] = "first",
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            根據指定欄位去除重複的資料列，並重設 index
        - Parameters:
            - df: pd.DataFrame
                要處理的資料表
            - subset: List[str]
                用來判斷重複的欄位名稱列表（Ex: ["year", "month", "stock_id"]）
            - keep: Union[str, bool]
                要保留哪一筆重複資料：
                    - "first": 保留第一筆（預設）
                    - "last": 保留最後一筆
                    - False: 移除所有重複的列
        - Return:
            - Optional[pd.DataFrame]
                去除重複值並重設 index 的資料表；輸入為 None 或空表時回傳 None
        """

        if df is None or df.empty:
            return None

        return df.drop_duplicates(subset=subset, keep=keep).reset_index(drop=True)

    @staticmethod
    def save_json(
        data: Any,
        file_path: Path,
        encoding: str = FileEncoding.UTF8.value,
        ensure_ascii: bool = False,
        indent: int = DEFAULT_JSON_INDENT,
    ) -> None:
        """
        - Description:
            將資料儲存成 JSON 檔案（父目錄不存在時一併建立）
        - Parameters:
            - data: Any
                要儲存的 Python 資料結構（如 dict 或 list）
            - file_path: Path
                儲存檔案的完整路徑
            - encoding: str
                檔案編碼（預設為 utf-8）
            - ensure_ascii: bool
                是否轉成 ASCII 編碼（預設 False，可保留中文）
            - indent: int
                JSON 排版的縮排層級（預設為 2）
        """

        file_path.parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)

    @staticmethod
    def load_json(file_path: Path, encoding: str = FileEncoding.UTF8.value) -> Any:
        """
        - Description:
            從指定 JSON 檔案讀取資料
        - Parameters:
            - file_path: Path
                JSON 檔案的完整路徑
            - encoding: str
                檔案編碼（預設為 utf-8）
        - Return:
            - Any
                從 JSON 載入的 Python 資料（通常為 dict 或 list）；
                **檔案不存在或格式錯誤時記錄錯誤並回 None**，由呼叫端決定要不要中止
        """

        try:
            with open(file_path, encoding=encoding) as f:
                data: Any = json.load(f)
                return data
        except FileNotFoundError:
            logger.error(f"找不到檔案: {file_path}")
            return None
        except json.JSONDecodeError:
            logger.error(f"JSON 格式錯誤: {file_path}")
            return None
