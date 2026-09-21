import csv
import datetime
import re
from io import StringIO
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

import pandas as pd
from loguru import logger

from core.config import FUTURES_MARGIN_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.exceptions import AnnouncementParseError
from core.utils import FUTURES_MULTIPLIER, FileEncoding

"""
台期貨保證金清洗（指數類 ＋ 股票類）

**兩支來源、三條產出**，分表依據是「金額 vs 比例」而不是「指數 vs 股票」：

| 來源 | 段落 | 產出 |
|------|------|------|
| 指數類一覽表 | 全份 | 每口金額 → `futures_margin_history` |
| 股票類一覽表 | 一(一) 股期（股票） | **適用比例** → `stock_futures_margin_rate_history` |
| 股票類一覽表 | 一(二) 股期（ETF） | **每口金額** → `futures_margin_history`（與指數期貨同表） |
| **調整公告附件** | 全份 | **歷史**每口金額 → `futures_margin_history`（`source='announcement'`） |

ETF 股期給的是每口固定金額，語意與臺股期貨相同；若因為它掛在「股票類」檔案裡
就塞進比例表，比例欄會永遠是 NULL。

---

**共通的四個坑**（兩支來源都適用）：

1. **生效日取自「更新日期」，不是抓取日**
    那是這組保證金**開始適用**的日子。同一組保證金連抓 30 天只會產生 1 列
    （主鍵相同被 loader 的 `INSERT OR IGNORE` 擋掉），本表是**變動序列**
    不是每日快照——這正是它能回答「某日生效的保證金是多少」的原因。
    解析不到一律整批放棄，**不可退回今天**。

2. **一律用 `csv` 模組解析，不可 `split(",")`**
    每列尾端有多餘的逗號，且股票類的公司名稱含 `"...Co., Ltd."`；
    用 split 會靜默錯位（實測把 `Ltd.` 讀成級距值）。

3. **表頭對不上就中止**，那代表站方改版；硬解只會把錯的數字寫進去。

4. **代碼帶尾端空白**（`DFF    `），必須 strip 才對得回 `futures_stock_universe`。

---

**指數類專屬**：

5. **契約乘數比例是免費的正確性檢查，但只在同一標的指數內成立**
    同一個標的指數的大小台，保證金與乘數等比例——2026-09-01 實測
    加權指數 TX(200) 701,000／MTX(50) 175,250／TMF(10) 35,050 皆為每點 3,505 元。
    **但跨指數不成立**：電子每點 244.50、金融每點 158.00，因為那是三個不同的指數。
    故檢查必須**分家族做**，拿 TX 去比 TE 會誤判成解析錯誤。

6. **只收得進 `FUTURES_MULTIPLIER` 的商品**
    一覽表含選擇權的風險保證金 A／B／C 值與尚未登錄乘數的商品；
    前者語意完全不同（不是每口金額），後者連 PnL 都算不出來。

---

**股票類專屬**：

7. **一份 CSV 有四個段落，欄位語意與更新日期都不同**（2026-09-01 實查）

    | 段落 | 內容 | 欄位型態 | 該段更新日期 |
    |------|------|----------|--------------|
    | 一(一) | 股期（標的為**股票**） | **適用比例** ＋ 級距 | 2026/08/28 |
    | 一(二) | 股期（標的為 **ETF**） | **每口固定金額** | 2026/08/12 |
    | 二(一) | 股**選擇權**（股票） | 比例，`a%`／`b%` 雙欄 | 2026/08/14 |
    | 二(二) | 股選擇權（ETF） | `A值`／`B值`，一商品佔兩列 | 2026/08/12 |

    **選擇權兩段一律不收**（語意是風險保證金參數，不是每口保證金），且要
    **先切掉「二、」之後的全部內容再解析**——選擇權代碼與股期只差一個字母
    （`DFF` vs `DFO`、`NYF` vs `NYA`），混進來完全不會報錯。
    **生效日逐段各自解析**：用第一個找到的日期套用到全部會讓 ETF 段錯 16 天。

8. **級距欄可以是空的，而且那不是解析錯誤**
    2026-09-01 實查 296 檔中有 15 檔（台玻、旺宏、南亞科、穩懋…）級距為空且
    比例更高（21.60%／22.95%／30.38%），那是**處置／注意股票的加成措施**。
    正常三個級距是 13.50%／16.20%／20.25%。空級距一律存 NULL，
    **不可因此把該檔丟掉**。
"""

# 「更新日期:2026/08/12」——冒號可能是全形或半形
EFFECTIVE_DATE_PATTERN: str = r"更新日期[:：]\s*(\d{4})/(\d{1,2})/(\d{1,2})"

# 指數類一覽表的表頭欄位（第二行）
INDEX_MARGIN_HEADER: List[str] = [
    "商品別",
    "結算保證金",
    "維持保證金",
    "原始保證金",
]

# 中文商品名 → `FUTURES_MULTIPLIER` 的契約代碼。
#
# **來源沒有代碼欄，只有中文名**，故此表不可省。名稱以 TAIFEX 一覽表 2026-09-01
# 的實際用字為準（「小型臺指」沒有「期貨」二字，「微型臺指期貨」有），
# 對不上的一律跳過而不猜——猜錯會把某商品的保證金掛到另一個商品上。
PRODUCT_NAME_TO_CODE: Dict[str, str] = {
    "臺股期貨": "TX",
    "小型臺指": "MTX",
    "微型臺指期貨": "TMF",
    "電子期貨": "TE",
    "小型電子期貨": "ZEF",
    "金融期貨": "TF",
    "小型金融期貨": "ZFF",
}

# 契約代碼 → 標的指數家族。
#
# **只用於乘數比例檢查**（見本檔說明第 3 點）：同一家族的大小台保證金與乘數等比例，
# 跨家族則否。不屬於任何家族的商品會被跳過檢查而非判為錯誤。
PRODUCT_INDEX_FAMILY: Dict[str, str] = {
    "TX": "TAIEX",
    "MTX": "TAIEX",
    "TMF": "TAIEX",
    "TE": "ELECTRONIC",
    "ZEF": "ELECTRONIC",
    "TF": "FINANCE",
    "ZFF": "FINANCE",
}

# 段落標記；以「開頭是否為此字串」比對，避免全形空白等細節差異
FUTURES_SECTION_MARKER: str = "一、股票期貨契約保證金一覽表"
OPTIONS_SECTION_MARKER: str = "二、股票選擇權契約保證金一覽表"
STOCK_UNDERLYING_MARKER: str = "(一) 標的證券為股票"
ETF_UNDERLYING_MARKER: str = "(二) 標的證券為受益憑證"

# 一(一) 的表頭：比例型
RATE_HEADER: List[str] = [
    "序號",
    "股票期貨英文代碼",
    "股票期貨標的證券代號",
    "股票期貨中文簡稱",
    "股票期貨標的證券",
    "保證金所屬級距",
    "結算保證金適用比例",
    "維持保證金適用比例",
    "原始保證金適用比例",
]

# 調整公告附件的表頭。2026-09-01 盤點 2020~2026 的 81 份附件，**80 份用這一種**；
# 另 1 份（2025/06/04）是「新增商品」而非調整，只有前六欄（沒有「調整前」）。
#
# ⚠️ **金額欄的順序與一覽表相反**：一覽表是「結算→維持→原始」，
# 公告是「原始→維持→結算」。照抄一覽表的取值位置會讓三個數字互換而不會報錯。
#
# ⚠️⚠️ **同一個表頭底下有兩種值**（2026-09-01 實測踩到）：指數期貨／ETF 期貨的
# 公告給「每口金額」（`526000`），**股票期貨的公告給「適用比例」**（`0.27`），
# 標題與欄位名完全相同，只能**看值有沒有小數點**來分。把 `0.27` 當成 27 元
# 寫進金額表不會報錯，只會讓保證金差六個數量級。
ANNOUNCEMENT_HEADER: List[str] = [
    "契約中文簡稱",
    "契約代碼",
    "契約ABC值",
    "調整後原始保證金",
    "調整後維持保證金",
    "調整後結算保證金",
]

# 生效日：標題的「並自115年4月1日…起實施」，民國年
ROC_EFFECTIVE_DATE_PATTERN: str = (
    r"自\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
)
ROC_YEAR_OFFSET: int = 1911


# 一(二) 的表頭：金額型
AMOUNT_HEADER: List[str] = [
    "序號",
    "股票期貨英文代碼",
    "股票期貨標的證券代號",
    "股票期貨中文簡稱",
    "股票期貨標的證券",
    "結算保證金",
    "維持保證金",
    "原始保證金",
]


class MarginSection(NamedTuple):
    """一個段落的解析結果：生效日 ＋ 資料列"""

    effective_date: datetime.date
    rows: List[List[str]]


class FuturesMarginCleaner(BaseDataCleaner):
    """Futures Margin Cleaner（指數類 ＋ 股票類）"""

    # 乘數比例檢查的容許誤差；TAIFEX 的數值是整數且等比例，實務上誤差為 0
    MULTIPLIER_RATIO_TOLERANCE: float = 1e-6

    # 生效日與公告日的最大合理落差（天）；超過視為標題解析錯誤
    MAX_EFFECTIVE_DATE_LAG: int = 30

    def __init__(self) -> None:
        super().__init__()

        # Downloads directory Path
        self.margin_dir: Path = FUTURES_MARGIN_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        # 金額型的欄位（指數期貨與 ETF 股期共用）；
        # 主鍵為 (effective_date, product)，本表是變動序列不是每日快照
        self.margin_cleaned_cols: List[str] = [
            "effective_date",
            "product",
            "product_name",
            "結算保證金",
            "維持保證金",
            "原始保證金",
            "source",
        ]
        # 比例型的欄位（股票股期）；主鍵為 (effective_date, product_id)
        self.rate_cleaned_cols: List[str] = [
            "effective_date",
            "product_id",
            "underlying_stock_id",
            "product_name",
            "保證金所屬級距",
            "結算保證金適用比例",
            "維持保證金適用比例",
            "原始保證金適用比例",
            "source",
        ]

        # 公告附件多帶「調整前」三欄，供鏈式驗證用；**入庫時只取 margin_cleaned_cols**
        self.announcement_cleaned_cols: List[str] = self.margin_cleaned_cols + [
            "調整前結算保證金",
            "調整前維持保證金",
            "調整前原始保證金",
        ]

        # 公告的比例列：不提供級距與標的證券代號，且「調整前」只留原始比例
        self.announcement_rate_cleaned_cols: List[str] = self.rate_cleaned_cols + [
            "調整前原始保證金適用比例",
        ]

        self.margin_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def parse_effective_date(text: str) -> Optional[datetime.date]:
        """
        - Description:
            自 CSV 原文解析「更新日期」作為生效日

            **解析不到一律回傳 None 而不退回今天**：退回今天會產生一列
            日期錯誤但看起來正常的資料，那比整批不入庫難查得多。
        - Parameters:
            - text: str
                CSV 原文
        - Return:
            - Optional[datetime.date]
                生效日；解析不到時為 None
        """

        match: Optional[re.Match] = re.search(EFFECTIVE_DATE_PATTERN, text)
        if match is None:
            return None

        year, month, day = (int(g) for g in match.groups())
        try:
            return datetime.date(year, month, day)
        except ValueError:
            return None

    def clean_index_margin(self, text: str) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗股價指數類的保證金一覽表
        - Parameters:
            - text: str
                crawler 取得的 CSV 原文（已 big5 解碼）
        - Return:
            - Optional[pd.DataFrame]
                清洗後的 DataFrame；生效日解析失敗、表頭不符或無有效列時為 None
        """

        if not text or not text.strip():
            logger.warning("[Futures Margin] 指數類 CSV 為空")
            return None

        effective_date: Optional[datetime.date] = self.parse_effective_date(text)
        if effective_date is None:
            # 沒有生效日就沒有主鍵，整批放棄
            logger.warning("[Futures Margin] 指數類 CSV 找不到更新日期，中止")
            return None

        rows: List[List[str]] = [
            [cell.strip() for cell in row]
            for row in csv.reader(StringIO(text))
            if any(cell.strip() for cell in row)
        ]

        header_index: Optional[int] = self.find_header_index(rows)
        if header_index is None:
            logger.warning(
                f"[Futures Margin] 指數類 CSV 表頭不符（預期 {INDEX_MARGIN_HEADER}），"
                f"可能為站方改版，中止"
            )
            return None

        records: List[Dict[str, object]] = []
        skipped: List[str] = []

        for row in rows[header_index + 1 :]:
            if len(row) < len(INDEX_MARGIN_HEADER):
                continue

            product_name: str = row[0]
            product: Optional[str] = PRODUCT_NAME_TO_CODE.get(product_name)
            if product is None:
                # 選擇權的 A／B／C 值與未登錄乘數的商品，見本檔說明第 4 點
                skipped.append(product_name)
                continue

            amounts: Optional[List[int]] = self.parse_amounts(row[1:4])
            if amounts is None:
                skipped.append(product_name)
                continue

            records.append(
                {
                    "effective_date": effective_date,
                    "product": product,
                    "product_name": product_name,
                    "結算保證金": amounts[0],
                    "維持保證金": amounts[1],
                    "原始保證金": amounts[2],
                    "source": "snapshot",
                }
            )

        if not records:
            logger.warning("[Futures Margin] 指數類 CSV 無有效列")
            return None

        if skipped:
            logger.info(
                f"* 未收錄 {len(skipped)} 項（選擇權風險保證金或乘數未登錄）：{skipped}"
            )

        df: pd.DataFrame = pd.DataFrame(records)[self.margin_cleaned_cols]

        if not self.check_multiplier_ratio(df):
            logger.warning(
                "[Futures Margin] 保證金與契約乘數的比例關係不成立，"
                "可能是欄位錯位或站方改制，中止入庫"
            )
            return None

        csv_path: Path = self.margin_dir / f"futures_margin_{effective_date:%Y%m%d}.csv"
        df.to_csv(csv_path, index=False, encoding=FileEncoding.UTF8_SIG.value)

        return df

    @staticmethod
    def find_header_index(rows: List[List[str]]) -> Optional[int]:
        """找出表頭所在的列；找不到回傳 None（代表版面改制）"""

        for i, row in enumerate(rows):
            if row[: len(INDEX_MARGIN_HEADER)] == INDEX_MARGIN_HEADER:
                return i
        return None

    @staticmethod
    def parse_amounts(cells: List[str]) -> Optional[List[int]]:
        """
        把三個金額欄轉成整數；任一欄不是純數字就回傳 None

        來源的數字不帶千分位，但仍先移除逗號以防站方改格式。
        """

        amounts: List[int] = []
        for cell in cells:
            digits: str = cell.replace(",", "").strip()
            if not digits.isdigit():
                return None
            amounts.append(int(digits))
        return amounts

    def check_multiplier_ratio(self, df: pd.DataFrame) -> bool:
        """
        - Description:
            以契約乘數的比例關係驗證解析結果（**分標的指數家族**）

            同一個標的指數的大小台，保證金與乘數等比例，故
            `原始保證金 / 乘數`（每點保證金）在家族內應為同一個值。
            欄位錯位或抓錯欄時這個關係立刻破裂。

            **不可跨家族比對**：2026-09-01 實測加權每點 3,505、電子 244.50、
            金融 158.00，三者本來就不同——拿 TX 去比 TE 會誤判成解析錯誤。

            只有一個成員的家族沒有可比對的對象，直接視為通過。
        - Parameters:
            - df: pd.DataFrame
                清洗後的 DataFrame
        - Return:
            - bool
                比例關係是否成立
        """

        per_point_by_family: Dict[str, List[float]] = {}
        for _, row in df.iterrows():
            family: Optional[str] = PRODUCT_INDEX_FAMILY.get(row["product"])
            if family is None:
                continue
            per_point: float = row["原始保證金"] / FUTURES_MULTIPLIER[row["product"]]
            per_point_by_family.setdefault(family, []).append(per_point)

        for family, values in per_point_by_family.items():
            baseline: float = values[0]
            if any(
                abs(value - baseline) > self.MULTIPLIER_RATIO_TOLERANCE
                for value in values
            ):
                logger.warning(
                    f"[Futures Margin] {family} 家族的每點保證金不一致：{values}"
                )
                return False

        return True

    def clean_stock_margin(
        self, text: str
    ) -> Optional[Dict[str, Optional[pd.DataFrame]]]:
        """
        - Description:
            清洗股票期貨保證金一覽表，回傳比例與金額兩個 DataFrame

            **選擇權段落一律不解析**，在切段時就被丟掉。
        - Parameters:
            - text: str
                crawler 取得的 CSV 原文（已 big5 解碼）
        - Return:
            - Optional[Dict[str, Optional[pd.DataFrame]]]
                `{"rate": ..., "amount": ...}`；整份無法解析時為 None。
                個別段落解析不出時該項為 None，另一項仍會回傳——兩段的
                更新日期不同、來源獨立，一段壞掉不該讓另一段也不入庫。
        """

        if not text or not text.strip():
            logger.warning("[Stock Futures Margin] CSV 為空")
            return None

        rows: List[List[str]] = [
            [cell.strip() for cell in row] for row in csv.reader(StringIO(text))
        ]

        futures_rows: List[List[str]] = self.take_futures_section(rows)
        if not futures_rows:
            logger.warning("[Stock Futures Margin] 找不到股票期貨段落，可能為站方改版")
            return None

        rate_section: Optional[MarginSection] = self.take_subsection(
            futures_rows, STOCK_UNDERLYING_MARKER, RATE_HEADER
        )
        amount_section: Optional[MarginSection] = self.take_subsection(
            futures_rows, ETF_UNDERLYING_MARKER, AMOUNT_HEADER
        )

        return {
            "rate": self.build_rate_df(rate_section),
            "amount": self.build_amount_df(amount_section),
        }

    @staticmethod
    def take_futures_section(rows: List[List[str]]) -> List[List[str]]:
        """
        取出「一、股票期貨」段落，丟掉「二、股票選擇權」之後的全部內容

        選擇權的代碼（`DFO`／`CAO`／`NYA`…）與股期高度相似，不先切掉的話
        很容易混進來——它們的保證金語意是風險保證金參數，不是每口保證金。
        """

        start: Optional[int] = None
        end: int = len(rows)

        for i, row in enumerate(rows):
            if not row or not row[0]:
                continue
            if row[0].startswith(FUTURES_SECTION_MARKER):
                start = i
            elif row[0].startswith(OPTIONS_SECTION_MARKER):
                end = i
                break

        return [] if start is None else rows[start:end]

    @staticmethod
    def take_subsection(
        rows: List[List[str]], marker: str, header: List[str]
    ) -> Optional[MarginSection]:
        """
        - Description:
            自段落內取出指定子節的生效日與資料列

            **生效日逐節各自解析**：四個子節的更新日期不同，用全檔第一個日期
            套用到全部會讓 ETF 股期的生效日錯 16 天（2026-09-01 實查）。
        - Parameters:
            - rows: List[List[str]]
                段落內的所有列
            - marker: str
                子節標題的起始字串
            - header: List[str]
                該子節的表頭；對不上代表站方改版
        - Return:
            - Optional[MarginSection]
                生效日與資料列；找不到子節或表頭不符時為 None
        """

        start: Optional[int] = None
        for i, row in enumerate(rows):
            if row and row[0].startswith(marker):
                start = i
                break
        if start is None:
            return None

        effective_date: Optional[datetime.date] = None
        header_index: Optional[int] = None

        for i in range(start + 1, len(rows)):
            row = rows[i]
            if not row or not any(row):
                continue
            # 下一個子節開始，代表本子節沒有表頭
            if row[0].startswith("(") and i != start:
                break
            if effective_date is None:
                found: Optional[re.Match] = re.search(EFFECTIVE_DATE_PATTERN, row[0])
                if found:
                    year, month, day = (int(g) for g in found.groups())
                    effective_date = datetime.date(year, month, day)
                    continue
            if row[: len(header)] == header:
                header_index = i
                break

        if effective_date is None or header_index is None:
            return None

        data_rows: List[List[str]] = []
        for row in rows[header_index + 1 :]:
            if not row or not any(row):
                continue
            # 遇到下一個子節標題就停
            if row[0].startswith("("):
                break
            if not row[0].isdigit():
                continue
            data_rows.append(row)

        return MarginSection(effective_date=effective_date, rows=data_rows)

    def build_rate_df(self, section: Optional[MarginSection]) -> Optional[pd.DataFrame]:
        """把一(一) 的比例列組成 DataFrame；比例一律轉成**小數**"""

        if section is None or not section.rows:
            logger.warning("[Stock Futures Margin] 比例段落解析不出資料")
            return None

        records: List[Dict[str, object]] = []
        for row in section.rows:
            if len(row) < len(RATE_HEADER):
                continue
            rates: Optional[List[float]] = self.parse_rates(row[6:9])
            if rates is None:
                continue
            records.append(
                {
                    "effective_date": section.effective_date,
                    "product_id": row[1],
                    "underlying_stock_id": row[2],
                    "product_name": row[3],
                    # 空級距存 None：處置股票沒有級距但仍有（更高的）比例，
                    # 見本檔說明第 3 點。**pandas 會把它正規化成 NaN**，
                    # 入庫時由 sqlite3 轉成真正的 NULL（已驗證）
                    "保證金所屬級距": row[5] or None,
                    "結算保證金適用比例": rates[0],
                    "維持保證金適用比例": rates[1],
                    "原始保證金適用比例": rates[2],
                    "source": "snapshot",
                }
            )

        if not records:
            return None

        df: pd.DataFrame = pd.DataFrame(records)[self.rate_cleaned_cols]
        self.save_csv(df, f"stock_futures_margin_rate_{section.effective_date:%Y%m%d}")
        return df

    def build_amount_df(
        self, section: Optional[MarginSection]
    ) -> Optional[pd.DataFrame]:
        """把一(二) 的金額列組成 DataFrame，欄位與 `futures_margin_history` 相同"""

        if section is None or not section.rows:
            logger.warning("[Stock Futures Margin] 金額段落解析不出資料")
            return None

        records: List[Dict[str, object]] = []
        for row in section.rows:
            if len(row) < len(AMOUNT_HEADER):
                continue
            amounts: Optional[List[int]] = self.parse_amounts(row[5:8])
            if amounts is None:
                continue
            records.append(
                {
                    "effective_date": section.effective_date,
                    "product": row[1],
                    "product_name": row[3],
                    "結算保證金": amounts[0],
                    "維持保證金": amounts[1],
                    "原始保證金": amounts[2],
                    "source": "snapshot",
                }
            )

        if not records:
            return None

        df: pd.DataFrame = pd.DataFrame(records)[self.margin_cleaned_cols]
        self.save_csv(df, f"etf_futures_margin_{section.effective_date:%Y%m%d}")
        return df

    @staticmethod
    def parse_rates(cells: List[str]) -> Optional[List[float]]:
        """
        `13.50%` → `0.1350`

        **存小數而非百分比數值**：下游直接乘不必再除以 100，
        而「忘記除 100」會讓保證金差 100 倍卻不會報錯。
        """

        rates: List[float] = []
        for cell in cells:
            value: str = cell.replace("%", "").replace(",", "").strip()
            try:
                rates.append(round(float(value) / 100, 6))
            except ValueError:
                return None
        return rates

    def save_csv(self, df: pd.DataFrame, stem: str) -> None:
        """中繼檔落地，供人工核對"""

        df.to_csv(
            self.margin_dir / f"{stem}.csv",
            index=False,
            encoding=FileEncoding.UTF8_SIG.value,
        )

    # === 調整公告 ===
    @classmethod
    def parse_announcement_effective_date(
        cls, title: str, announcement_date: datetime.date
    ) -> datetime.date:
        """
        - Description:
            自公告標題解析生效日（民國年）

            標題形如「…並自115年4月1日一般交易時段結束後起實施」。
            **生效日不是公告日**：TAIFEX 的規則是「自公告日之次一一般交易時段
            結束後起實施」，實測 2020/03/05 的公告生效日是 109/3/6。

            取**最後一個**符合的日期：標題前段常引用主管機關來函的日期
            （「依金管會115年6月10日…函」），但那些不帶「自」字。

            解析結果若不在 `[公告日, 公告日 + 30 天]` 內就視為解錯，退回公告日 +1
            並警告——標題裡混進其他日期時，用錯的日期會讓整條變動序列錯位。
        - Parameters:
            - title: str
                公告標題
            - announcement_date: datetime.date
                公告日
        - Return:
            - datetime.date
                生效日
        """

        fallback: datetime.date = announcement_date + datetime.timedelta(days=1)
        matches: List[tuple] = re.findall(ROC_EFFECTIVE_DATE_PATTERN, title)

        if not matches:
            logger.warning(
                f"[Futures Margin] {announcement_date} 標題無生效日，退回公告日 +1"
            )
            return fallback

        roc_year, month, day = (int(g) for g in matches[-1])
        try:
            effective_date = datetime.date(roc_year + ROC_YEAR_OFFSET, month, day)
        except ValueError:
            logger.warning(
                f"[Futures Margin] {announcement_date} 生效日不合法，退回公告日 +1"
            )
            return fallback

        if not (
            announcement_date
            <= effective_date
            <= announcement_date + datetime.timedelta(days=cls.MAX_EFFECTIVE_DATE_LAG)
        ):
            logger.warning(
                f"[Futures Margin] {announcement_date} 解出的生效日 {effective_date} "
                f"不在合理區間，退回公告日 +1"
            )
            return fallback

        return effective_date

    def clean_margin_announcement(
        self, text: str, title: str, announcement_date: datetime.date
    ) -> Optional[Dict[str, Optional[pd.DataFrame]]]:
        """
        - Description:
            清洗保證金調整公告的 CSV 附件

            **金額欄的順序與一覽表相反**（公告是原始→維持→結算），
            照抄一覽表的取值位置會讓三個數字互換而不會報錯。

            **只收期貨**：`契約ABC值` 欄非空者是選擇權的 A／B／C 值，
            語意不是每口保證金。
        - Parameters:
            - text: str
                附件 CSV 原文（已 big5 解碼）
            - title: str
                公告標題，用來解生效日
            - announcement_date: datetime.date
                公告日
        - Return:
            - Optional[Dict[str, Optional[pd.DataFrame]]]
                `{"margin": 金額列, "rate": 比例列}`，兩者皆含「調整前」欄供鏈式驗證；
                **入庫時只取各自的 cleaned_cols**。完全沒有期貨列、或附件不是
                保證金調整表時為 None
        - Raise:
            - AnnouncementParseError
                附件空白、是 HTML 頁面，或保證金附件的表頭改版
        """

        # **解析不了與「沒有期貨列」要分開**：前者拋出、由 updater 當成下載失敗
        # 下次重試；後者回 None、寫進處理紀錄不再下載。混在一起的話，期交所
        # 改版或下載到錯誤頁的那則公告會被記成「沒有期貨列」，從此不再重抓
        if not text or not text.strip():
            raise AnnouncementParseError(announcement_date, "附件內容是空的")
        if text.lstrip()[:1] == "<":
            raise AnnouncementParseError(announcement_date, "下載到的是 HTML 頁面")

        rows: List[List[str]] = [
            [cell.strip() for cell in row]
            for row in csv.reader(StringIO(text))
            if any(cell.strip() for cell in row)
        ]
        if not rows or rows[0][: len(ANNOUNCEMENT_HEADER)] != ANNOUNCEMENT_HEADER:
            header: List[str] = rows[0] if rows else []
            # 表頭有契約代碼與保證金欄、只是排列不同 → 保證金附件改版了；
            # 完全是別的欄位 → 部位限制等其他公告，沒有期貨保證金可收
            if "契約代碼" in header and any("保證金" in cell for cell in header):
                raise AnnouncementParseError(
                    announcement_date, f"保證金附件的表頭與預期不符：{header}"
                )
            logger.info(
                f"[Futures Margin] {announcement_date} 附件不是保證金調整表，跳過"
            )
            return None

        effective_date: datetime.date = self.parse_announcement_effective_date(
            title, announcement_date
        )

        margin_records: List[Dict[str, object]] = []
        rate_records: List[Dict[str, object]] = []

        for row in rows[1:]:
            if len(row) < len(ANNOUNCEMENT_HEADER):
                continue
            # 契約ABC值非空 → 選擇權的風險保證金參數，不是每口保證金
            if row[2]:
                continue

            # **同一個表頭底下有兩種值**：含小數點的是股票期貨的「適用比例」，
            # 純整數的是指數／ETF 期貨的「每口金額」。見本檔常數區的說明。
            if self.is_rate_row(row[3:6]):
                after_rates: Optional[List[float]] = self.parse_plain_rates(row[3:6])
                if after_rates is None:
                    continue
                before_rates: Optional[List[float]] = (
                    self.parse_plain_rates(row[6:9]) if len(row) >= 9 else None
                )
                rate_records.append(
                    {
                        "effective_date": effective_date,
                        "product_id": row[1],
                        "underlying_stock_id": None,
                        "product_name": row[0],
                        # 公告不提供級距，只給比例
                        "保證金所屬級距": None,
                        "結算保證金適用比例": after_rates[2],
                        "維持保證金適用比例": after_rates[1],
                        "原始保證金適用比例": after_rates[0],
                        "source": "announcement",
                        "調整前原始保證金適用比例": (
                            None if before_rates is None else before_rates[0]
                        ),
                    }
                )
                continue

            after: Optional[List[int]] = self.parse_amounts(row[3:6])
            if after is None:
                continue
            # 「新增商品」型的公告只有前六欄，沒有調整前
            before: Optional[List[int]] = (
                self.parse_amounts(row[6:9]) if len(row) >= 9 else None
            )

            margin_records.append(
                {
                    "effective_date": effective_date,
                    "product": row[1],
                    "product_name": row[0],
                    # 公告的順序是原始→維持→結算，此處還原成表的欄位語意
                    "結算保證金": after[2],
                    "維持保證金": after[1],
                    "原始保證金": after[0],
                    "source": "announcement",
                    "調整前結算保證金": None if before is None else before[2],
                    "調整前維持保證金": None if before is None else before[1],
                    "調整前原始保證金": None if before is None else before[0],
                }
            )

        if not margin_records and not rate_records:
            logger.info(
                f"* {announcement_date} 附件無期貨列（多為選擇權或部位限制公告），跳過"
            )
            return None

        result: Dict[str, Optional[pd.DataFrame]] = {"margin": None, "rate": None}

        if margin_records:
            df: pd.DataFrame = pd.DataFrame(margin_records)[
                self.announcement_cleaned_cols
            ]
            self.save_csv(df, f"futures_margin_announcement_{effective_date:%Y%m%d}")
            result["margin"] = df

        if rate_records:
            rate_df: pd.DataFrame = pd.DataFrame(rate_records)[
                self.announcement_rate_cleaned_cols
            ]
            self.save_csv(
                rate_df, f"stock_futures_margin_announcement_{effective_date:%Y%m%d}"
            )
            result["rate"] = rate_df

        return result

    @staticmethod
    def is_rate_row(cells: List[str]) -> bool:
        """
        判斷一列給的是比例還是金額

        **只看有沒有小數點**：保證金金額一律是整數元（最小也有數千），
        比例則一律小於 1（`0.27`）。這是兩者唯一可靠的區分方式——
        欄位名與表頭完全相同。
        """

        return any("." in cell for cell in cells)

    @staticmethod
    def parse_plain_rates(cells: List[str]) -> Optional[List[float]]:
        """
        公告裡的比例已經是小數（`0.27`），不像一覽表帶百分號

        故此處只做型別轉換，不再除以 100。
        """

        rates: List[float] = []
        for cell in cells:
            try:
                rates.append(round(float(cell.replace(",", "").strip()), 6))
            except ValueError:
                return None
        return rates
