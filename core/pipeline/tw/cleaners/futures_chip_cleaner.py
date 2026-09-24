import datetime
import io
from typing import List, Optional

import pandas as pd
from loguru import logger

from core.pipeline.shared.base_cleaner import BaseDataCleaner

"""
台期貨籌碼清洗器（三大法人、大額交易人、選擇權 PCR）

**欄位語言跟著來源走**：三個資料集都是
交易所直接給的中文欄名，一律原樣保留；只有**主鍵欄改成英文**
（`date`／`product_name`／`investor`／`product`／`expiry`／`trader_type`），
理由是主鍵會出現在每一句查詢裡。

三個資料集的形狀完全不同，故分三個 `clean_*`：

| 資料集 | 主鍵 | 一天幾列 |
|--------|------|----------|
| 三大法人 | (date, 商品名稱, 身份別) | 商品數 × 3 |
| 大額交易人 | (date, 商品, 到期月份, 交易人類別) | 約 1,400 |
| PCR | (date) | 1 |

**日期一律以來源的 `日期` 欄為準**（不是查詢日）：一次區間查詢的回應會涵蓋
一整個月，把 `date` 覆寫成查詢起日會讓整批資料的日期全錯——
而且錯得很整齊，看起來完全正常。

**三個來源格式的坑**：

1. **代碼欄有補空白**：大額交易人的 `商品(契約)` 是 `"BRF    "`、
   到期月份是 `"202610  "`，不 strip 的話主鍵會多出看不見的字元，
   join 一定對不上而且查不出原因。
2. **`999999` 與 `666666` 不是到期月份**：來源檔尾自己註明——`999999` 是
   「所有契約合計（含各週與各月）」、`666666` 是「所有週到期契約合計」、
   `yyyymm` 才是近月契約。保留它們但要知道語意，直接 `SUM()` 會把同一天的
   部位重複計算。交易人類別同理：`0` 是前五／十大交易人，`1` 是其中的**特定法人**
   （`1` 是 `0` 的子集，兩者相加沒有意義）。
3. **CSV 檔尾有三行說明文字**：它們沒有逗號分隔，pandas 會解析成
   「第一欄有值、其餘全 NaN」的資料列。清洗時 `date` 又會被覆寫成查詢日，
   於是變成**看起來完全合法、實際全是 NULL 的垃圾列**。2026-09-02 實測：
   每天多出 3 列這種東西，最後是被主鍵的 `NOT NULL` 擋下的——而 `INSERT OR IGNORE`
   會把它**靜靜吞掉**，只有「新增 1386 列（共 1389 列）」這個數字對不上會透露。
   故一律在清洗層以「主鍵欄不可為空」濾掉，不要仰賴資料庫替我們發現問題。
"""


class FuturesChipCleaner(BaseDataCleaner):
    """把三個籌碼資料集的 CSV 原文清成可入庫的 DataFrame"""

    # 大額交易人的「所有契約月份合計」列
    ALL_EXPIRY_CODE: str = "999999"

    # 三大法人的身份別（來源給中文，原樣保留）
    INVESTOR_TYPES: List[str] = ["自營商", "投信", "外資及陸資"]

    def __init__(self) -> None:
        super().__init__()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""
        pass

    def clean(
        self, raw: str, date: Optional[datetime.date] = None
    ) -> Optional[pd.DataFrame]:
        """預設路徑：清三大法人"""

        return self.clean_institutional(raw, date)

    @staticmethod
    def read_csv(raw: str) -> Optional[pd.DataFrame]:
        """
        把 CSV 原文讀成 DataFrame

        **所有欄位一律先當字串讀**：商品代碼補了空白、部位數帶千分位逗號，
        讓 pandas 自行推型別會在不同日期推出不同結果（有些日子整欄變 float）。

        **`index_col=False` 不可省**：PCR 的每一列結尾都多一個逗號，於是資料欄
        比表頭多一欄，pandas 會**自作主張把第一欄當成索引**——結果整列往左位移，
        賣權成交量變成買權成交量、比率欄變成 NaN，而且完全不會報錯。
        2026-09-02 實測踩到（308,922 被讀成 306,713）。
        """

        try:
            df: pd.DataFrame = pd.read_csv(io.StringIO(raw), dtype=str, index_col=False)
        except (pd.errors.ParserError, ValueError) as error:
            logger.warning(f"[Futures Chip] CSV 解析失敗：{error}")
            return None

        return None if df.empty else df

    @staticmethod
    def normalize_date(df: pd.DataFrame) -> pd.DataFrame:
        """
        把來源的 `日期`（`2026/08/28`）正規化成 ISO（`2026-08-28`）

        **用來源的日期而不是查詢日**：區間查詢一次回一整個月，用查詢起日覆寫
        會讓整批資料的日期全錯。轉不出日期的列（檔尾說明文字）直接丟掉。
        """

        parsed: pd.Series = pd.to_datetime(
            df["date"].astype(str).str.strip(), format="%Y/%m/%d", errors="coerce"
        )
        cleaned: pd.DataFrame = df[parsed.notna()].copy()
        cleaned["date"] = parsed[parsed.notna()].dt.strftime("%Y-%m-%d")
        return cleaned

    @staticmethod
    def drop_invalid_rows(df: pd.DataFrame, keys: List[str]) -> pd.DataFrame:
        """
        濾掉主鍵欄為空的列

        **這不是防禦性程式碼，是必要的**：來源檔尾三行說明文字會被 pandas 解析成
        資料列，而 `INSERT OR IGNORE` 遇到主鍵 `NOT NULL` 時不會發出任何訊息，
        於是這些垃圾列只會表現成入庫筆數對不上（見本檔說明的第 3 點）。
        """

        cleaned: pd.DataFrame = df.copy()
        for key in keys:
            if key == "date":
                continue
            cleaned = cleaned[
                cleaned[key].notna() & (cleaned[key].astype(str).str.strip() != "")
            ]
        return cleaned

    @staticmethod
    def to_numeric(df: pd.DataFrame, exclude: List[str]) -> pd.DataFrame:
        """把非主鍵欄轉成數字；千分位逗號先去掉，轉不動的留 NaN 不猜 0"""

        for column in df.columns:
            if column in exclude:
                continue
            df[column] = pd.to_numeric(
                df[column].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )
        return df

    def clean_institutional(
        self, raw: str, date: Optional[datetime.date] = None
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗三大法人：`(date, product_name, investor)` 為主鍵

            **商品以「名稱」而不是代碼入庫**：來源只給中文名（臺股期貨、電子期貨…），
            硬要在此對應成代碼就得維護一份猜測的對照表。要接回行情表時，
            以 `futures_margin_history` 的 `product`／`product_name` 對照即可
            ——那份對照是交易所自己給的。
        - Parameters:
            - raw: str
                CSV 原文（可含多天）
            - date: Optional[datetime.date]
                **僅為相容保留，不參與計算**——日期一律取自來源的 `日期` 欄
        - Return:
            - Optional[pd.DataFrame]
                清洗後的資料；無有效列時為 None
        """

        df: Optional[pd.DataFrame] = self.read_csv(raw)
        if df is None:
            return None

        df = df.rename(
            columns={"日期": "date", "商品名稱": "product_name", "身份別": "investor"}
        )
        if not {"date", "product_name", "investor"}.issubset(df.columns):
            logger.warning(f"[Futures Chip] 三大法人欄位與預期不符：{list(df.columns)}")
            return None

        for column in ("product_name", "investor"):
            df[column] = df[column].astype(str).str.strip()

        df = self.normalize_date(df)
        df = self.drop_invalid_rows(df, ["date", "product_name", "investor"])
        df = df[df["investor"].isin(self.INVESTOR_TYPES)]
        if df.empty:
            return None

        return self.to_numeric(df, exclude=["date", "product_name", "investor"])

    def clean_large_trader(
        self, raw: str, date: Optional[datetime.date] = None
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗大額交易人：`(date, product, expiry, trader_type)` 為主鍵

            **代碼欄一定要 strip**：來源是 `"BRF    "`／`"202610  "` 這種補空白的
            固定寬度格式，不處理的話主鍵會多出看不見的字元。
        - Parameters:
            - raw: str
                CSV 原文（可含多天）
            - date: Optional[datetime.date]
                **僅為相容保留，不參與計算**——日期一律取自來源的 `日期` 欄
        - Return:
            - Optional[pd.DataFrame]
                清洗後的資料；無有效列時為 None
        """

        df: Optional[pd.DataFrame] = self.read_csv(raw)
        if df is None:
            return None

        df = df.rename(
            columns={
                "日期": "date",
                "商品(契約)": "product",
                "商品名稱(契約名稱)": "product_name",
                "到期月份(週別)": "expiry",
                "交易人類別": "trader_type",
            }
        )
        required: set = {"date", "product", "expiry", "trader_type"}
        if not required.issubset(df.columns):
            logger.warning(
                f"[Futures Chip] 大額交易人欄位與預期不符：{list(df.columns)}"
            )
            return None

        for column in ("product", "product_name", "expiry", "trader_type"):
            if column in df.columns:
                df[column] = df[column].astype(str).str.strip()

        df = self.normalize_date(df)
        df = self.drop_invalid_rows(df, ["date", "product", "expiry", "trader_type"])
        if df.empty:
            return None

        return self.to_numeric(
            df, exclude=["date", "product", "product_name", "expiry", "trader_type"]
        )

    def clean_put_call_ratio(
        self, raw: str, date: Optional[datetime.date] = None
    ) -> Optional[pd.DataFrame]:
        """清洗選擇權 PCR：一天一列，主鍵只有 `date`"""

        df: Optional[pd.DataFrame] = self.read_csv(raw)
        if df is None:
            return None

        df = df.rename(columns={"日期": "date"})
        if "date" not in df.columns:
            logger.warning(f"[Futures Chip] PCR 欄位與預期不符：{list(df.columns)}")
            return None

        # 來源每列結尾有多餘逗號，pandas 會多出一個全空的欄位
        df = df.loc[
            :,
            [column for column in df.columns if not str(column).startswith("Unnamed")],
        ]
        df = self.normalize_date(df)
        if df.empty:
            return None

        return self.to_numeric(df, exclude=["date"])
