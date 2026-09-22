import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import CORPORATE_ACTION_DOWNLOADS_PATH
from core.pipeline.shared.base_cleaner import BaseDataCleaner
from core.pipeline.utils.exceptions import ColumnLayoutError
from core.utils import TimeUtils
from core.utils.constant import FileEncoding

"""
非除權息公司行動清洗器

把 TWSE 與 TPEX 兩種版面收斂成同一組欄位。兩邊給的是同一件事，但**每一個細節
都不一樣**，這正是要有 cleaner 的原因：

| 項目 | TWSE | TPEX |
|------|------|------|
| 日期格式 | `114/02/12` | `1140113`（無分隔） |
| 前收盤價欄名 | `停止買賣前收盤價格` | `最後交易日之收盤價格` |
| 參考價欄名 | `恢復買賣參考價` | `減資恢復買賣開始日參考價格` |
| 換股比例 | 無 | `詳細資料` HTML 內的「每壹仟股換發新股票」 |

**調整倍率 ＝ 恢復買賣參考價 ÷ 停止買賣前收盤價**。減資是**大於 1**（價格上調，
股數變少），分割是小於 1。這與 `dividend.還原係數`（恆 < 1）方向相反，
也是本表不能併進 `dividend` 的原因。

TPEX 的 `詳細資料` 提供「每壹仟股換發新股票」與「每股退還股款」，兩者合起來是
調整倍率的**獨立佐證**：

    恢復買賣參考價 ＝（停止買賣前收盤價 − 每股退還股款）× 1000 ÷ 換發股數

⚠️ **退還股款那一項不能省**。只用換股比例（`前收 × 1000/換發股數`）在「彌補虧損」
型減資會剛好對上，但「退還股款」型會整批對不上——實測 2025 年 10 筆有 2 筆
（3290 東浦、6167 久正）就是這樣，一度被誤判成資料有問題。補上退還股款後
四筆抽驗全部**與站方參考價分毫不差**（36.64／12.17／39.18／25.26）。

**退還股款不入表**：`調整倍率` ＝ 參考價 ÷ 前收盤價，本來就已經把它算進去了
（價格的跳空就是這麼來的）。退還的現金對**總報酬**另有意義，但那是股利型現金流，
與「把價格序列的假跳空補平」是兩件事，混進同一張表只會讓下一個人用錯。
"""

# 清洗後的統一欄位（與資料表一致）
OUTPUT_COLUMNS: List[str] = [
    "date",
    "stock_id",
    "證券名稱",
    "停止買賣前收盤價",
    "恢復買賣參考價",
    "調整倍率",
    "事件類型",
    "原因",
    "資料來源",
]

# 倍率與換股比例的容許誤差（相對值）。站方兩個數字各自四捨五入，不會完全相等
RATIO_TOLERANCE: float = 0.02

# 倍率偏離 1 超過這個比例才算「真的有調整」；等於 1 的事件不影響還原價
RATIO_SIGNIFICANT_DEVIATION: float = 0.001


class CorporateActionCleaner(BaseDataCleaner):
    """清洗上市／上櫃的減資（含面額變更）恢復買賣參考價"""

    # 兩邊各自的欄名對照（值是清洗後的統一欄名）
    TWSE_COLUMN_MAP: Dict[str, str] = {
        "恢復買賣日期": "date",
        "股票代號": "stock_id",
        "名稱": "證券名稱",
        "停止買賣前收盤價格": "停止買賣前收盤價",
        "恢復買賣參考價": "恢復買賣參考價",
        "減資原因": "原因",
    }
    TPEX_COLUMN_MAP: Dict[str, str] = {
        "恢復買賣日期": "date",
        "股票代號": "stock_id",
        "名稱": "證券名稱",
        "最後交易日之收盤價格": "停止買賣前收盤價",
        "減資恢復買賣開始日參考價格": "恢復買賣參考價",
        "減資原因": "原因",
    }

    def __init__(self) -> None:
        super().__init__()

        self.corporate_action_dir: Path = CORPORATE_ACTION_DOWNLOADS_PATH

        self.setup()

    def setup(self) -> None:
        """Set Up the Config of Cleaner"""

        self.corporate_action_dir.mkdir(parents=True, exist_ok=True)

    def clean(
        self,
        df: Optional[pd.DataFrame],
        source: str,
        file_name: str = "",
    ) -> Optional[pd.DataFrame]:
        """
        - Description:
            清洗單一來源的公司行動資料
        - Parameters:
            - df: Optional[pd.DataFrame]
                crawler 產出的原始表
            - source: str
                `twse` 或 `tpex`
            - file_name: str
                指定檔名時另存一份 CSV；空字串則只回傳不落地
        - Return:
            - Optional[pd.DataFrame]
                清洗後的資料；無有效列時為 None
        - Raise:
            - ColumnLayoutError
                欄名對不上（來源版面改制）
        """

        if df is None or df.empty:
            return None

        column_map: Dict[str, str] = (
            self.TWSE_COLUMN_MAP if source == "twse" else self.TPEX_COLUMN_MAP
        )

        missing: List[str] = [key for key in column_map if key not in df.columns]
        if missing:
            # 欄名對不上代表版面改制。**不硬取位置**：欄位錯位是靜默的錯，會一路錯到
            # 還原價。也不回 None：那在 updater 端與「區間內沒有事件」長得一樣
            raise ColumnLayoutError(
                f"[corporate_action] {source} 缺欄位 {missing}",
                expected=len(column_map),
                actual=len(column_map) - len(missing),
                columns=list(df.columns),
            )

        work: pd.DataFrame = df.rename(columns=column_map).copy()

        work["date"] = work["date"].apply(self.parse_roc_date)
        work["stock_id"] = work["stock_id"].astype(str).str.strip()
        work["證券名稱"] = work["證券名稱"].astype(str).str.strip()
        work["原因"] = work["原因"].astype(str).str.strip()

        for column in ("停止買賣前收盤價", "恢復買賣參考價"):
            work[column] = pd.to_numeric(
                work[column].astype(str).str.replace(",", "", regex=False),
                errors="coerce",
            )

        before: int = len(work)
        work = work.dropna(
            subset=["date", "stock_id", "停止買賣前收盤價", "恢復買賣參考價"]
        )
        # 前收盤價為 0 無法算倍率（站方以 `--` 表示無資料，已被轉成 NaN）
        work = work[work["停止買賣前收盤價"] > 0]
        if len(work) < before:
            logger.info(
                f"[corporate_action] {source}: {before} 筆中 {before - len(work)} "
                "筆缺日期或價格，已略過"
            )

        if work.empty:
            return None

        work["調整倍率"] = (work["恢復買賣參考價"] / work["停止買賣前收盤價"]).round(6)
        work["資料來源"] = source
        work["事件類型"] = work.apply(
            lambda row: self.classify_event(
                reason=row["原因"], ratio=row["調整倍率"], source=source
            ),
            axis=1,
        )

        if source == "tpex":
            self._cross_check_tpex_ratio(df, work)

        work = self._dedup(work, source)

        result: pd.DataFrame = work[OUTPUT_COLUMNS].sort_values(
            ["date", "stock_id"], kind="stable"
        )

        if file_name:
            self.save(result, file_name)
        return result

    def save(self, df: pd.DataFrame, file_name: str) -> None:
        """把清洗結果落地為 CSV（loader 掃這個目錄入庫）"""

        path: Path = self.corporate_action_dir / file_name
        df.to_csv(path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"[corporate_action] 已存檔 {path}（{len(df)} 筆）")

    @staticmethod
    def classify_event(reason: str, ratio: float, source: str) -> str:
        """
        - Description:
            判斷事件類型

            ⚠️ **不可用倍率方向判斷是不是分割**。這兩個端點本身就是**減資**端點，
            分割不會出現在裡面；而「退還股款」型減資的參考價可能**低於**前收盤價
            ——退還的現金比股數合併的效果還大時就會這樣。實測 632 筆裡有 7 筆
            （協禧 0.922、無敵 0.934、中環 0.950…）原因欄明寫「退還股款」或
            「現金減資」，卻因倍率 < 1 被誤判成分割。

            所以端點來源一律看 `原因` 欄；`detected` 來源才用倍率方向分辨
            分割（價格下調）與反向分割（價格上調）。
        - Parameters:
            - reason: str
                站方的 `減資原因`
            - ratio: float
                調整倍率
            - source: str
                `twse`／`tpex`／`detected`
        - Return:
            - str
                事件類型
        """

        if "面額" in str(reason):
            return "面額變更"

        if source in ("twse", "tpex"):
            # 端點就是減資端點，不會有分割
            return "減資"

        # detected：只有倍率方向可用
        if ratio < 1 - RATIO_SIGNIFICANT_DEVIATION:
            return "分割"
        if ratio > 1 + RATIO_SIGNIFICANT_DEVIATION:
            return "反向分割"
        return "減資"

    @staticmethod
    def parse_roc_date(value: object) -> Optional[datetime.date]:
        """
        - Description:
            解析民國日期，同時吃 TWSE 的 `114/02/12` 與 TPEX 的 `1140113`

            兩種格式收在同一個函式裡，因為它們指的是同一個欄位（`恢復買賣日期`），
            分成兩支只會讓呼叫端要記得自己在哪一邊。
        - Parameters:
            - value: object
                站方回傳的日期字串
        - Return:
            - Optional[datetime.date]
                無法解析時為 None（由呼叫端 dropna）
        """

        text: str = str(value).strip()
        if not text:
            return None

        try:
            if "/" in text:
                roc_year, month, day = text.split("/")
            elif text.isdigit() and len(text) == 7:
                roc_year, month, day = text[:3], text[3:5], text[5:7]
            else:
                return None
            year: int = int(TimeUtils.convert_roc_to_ad_year(roc_year))
            return datetime.date(year, int(month), int(day))
        except (ValueError, TypeError):
            # 只收「這個字串不是日期」：拆不出三段、月日非數字、日期超出範圍，
            # 以及 `convert_roc_to_ad_year()` 對無效年份拋的 ValueError。
            # 原本是 `except Exception`，連 schema 變更造成的錯誤都會被清成 None
            return None

    @staticmethod
    def _extract_detail_number(detail_html: object, label: str) -> Optional[float]:
        """由 TPEX 的 `詳細資料` HTML 取出某個標籤後面的數字；取不到回 None"""

        match = re.search(rf"{label}[^0-9]*([0-9]+\.?[0-9]*)", str(detail_html))
        if match is None:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @classmethod
    def extract_tpex_share_exchange(cls, detail_html: object) -> Optional[float]:
        """取出「每壹仟股換發新股票」的股數（例：550 ⇒ 1000 股換 550 股）"""

        shares: Optional[float] = cls._extract_detail_number(
            detail_html, "每壹仟股換發新股票"
        )
        return shares if shares and shares > 0 else None

    @classmethod
    def extract_tpex_cash_refund(cls, detail_html: object) -> float:
        """
        取出「每股退還股款」（元／股）；沒有這一項時為 0

        **退還股款型減資少了這一項就會對不上**：站方的參考價是
        `(前收 − 退還股款) × 1000 / 換發股數`，不是單純的換股比例。
        """

        refund: Optional[float] = cls._extract_detail_number(
            detail_html, "每股退還股款"
        )
        return refund if refund is not None else 0.0

    def _cross_check_tpex_ratio(
        self, raw_df: pd.DataFrame, cleaned: pd.DataFrame
    ) -> None:
        """
        - Description:
            以 TPEX 的換股比例交叉驗證調整倍率，不符只記警告

            **只警告不擋**：兩個數字都由站方四捨五入而來，偶爾對不上未必是錯；
            但若整批都對不上，那就是欄位語意變了，警告會讓人看得見。
        - Parameters:
            - raw_df: pd.DataFrame
                含 `詳細資料` 的原始表
            - cleaned: pd.DataFrame
                已算出 `調整倍率` 的清洗結果（與 raw_df 同索引）
        """

        if "詳細資料" not in raw_df.columns:
            return

        mismatched: int = 0
        checked: int = 0
        for index, ratio in cleaned["調整倍率"].items():
            detail: object = raw_df.at[index, "詳細資料"]
            shares: Optional[float] = self.extract_tpex_share_exchange(detail)
            if shares is None:
                continue
            checked += 1

            # 站方公式：(前收 − 每股退還股款) × 1000 / 換發股數
            previous_close: float = float(cleaned.at[index, "停止買賣前收盤價"])
            refund: float = self.extract_tpex_cash_refund(detail)
            expected_reference: float = (previous_close - refund) * 1000.0 / shares
            expected: float = expected_reference / previous_close

            if abs(ratio - expected) / expected > RATIO_TOLERANCE:
                mismatched += 1
                logger.warning(
                    f"[corporate_action] tpex {cleaned.at[index, 'stock_id']} "
                    f"{cleaned.at[index, 'date']}：倍率 {ratio:.4f} 與"
                    f"（前收 {previous_close} − 退還股款 {refund}）× 1000/{shares} "
                    f"推得的 {expected:.4f} 不符"
                )

        if checked:
            logger.info(
                f"[corporate_action] tpex 換股比例交叉驗證：{checked} 筆中 "
                f"{mismatched} 筆不符"
            )

    @staticmethod
    def _dedup(df: pd.DataFrame, source: str) -> pd.DataFrame:
        """
        - Description:
            同一個 `(date, stock_id)` 去重

            **站方確實會給重複列**：公司改名期間同一筆事件會出現兩次，名稱不同、
            價格相同（實測南亞科 2014-09-09 以「南亞科」與「南科」各出現一次，
            2013~2026 上市共 5 筆這種多餘列）。主鍵是 `(date, stock_id)`，
            不先去重就會在入庫時撞鍵。

            **價格不同才是真的衝突**：那時記警告，因為那代表站方對同一件事給了
            兩個答案，不是單純改名。
        - Parameters:
            - df: pd.DataFrame
                清洗後、尚未去重的資料
            - source: str
                來源名稱，只用於訊息
        - Return:
            - pd.DataFrame
                去重後的資料
        """

        key: List[str] = ["date", "stock_id"]
        duplicated = df[df.duplicated(subset=key, keep=False)]

        if not duplicated.empty:
            for _, group in duplicated.groupby(key):
                if group["調整倍率"].nunique() > 1:
                    logger.warning(
                        f"[corporate_action] {source} {group.iloc[0]['stock_id']} "
                        f"{group.iloc[0]['date']}：同一鍵有 "
                        f"{group['調整倍率'].nunique()} 種倍率，保留第一筆"
                    )
            logger.info(
                f"[corporate_action] {source}: 去重移除 "
                f"{len(df) - df.drop_duplicates(subset=key).shape[0]} 筆重複列"
            )

        return df.drop_duplicates(subset=key, keep="first")
