from dataclasses import asdict, dataclass
from typing import Dict, Optional

"""公開資訊觀測站（MOPS）查詢表單的 POST payload"""


@dataclass
class Payload:
    """公開資訊觀測站（MOPS）查詢表單的 HTTP payload 結構"""

    firstin: Optional[str] = "1"  # 表單首次送出旗標（站方預設 1）
    step: Optional[str] = "1"  # 查詢步驟（站方預設 1）
    TYPEK: Optional[str] = (
        None  # {sii: 上市, otc: 上櫃, all: 全部, sii0: 國內上市, sii1: 國外上市, otc0: 國內上櫃, otc1: 國外上貴}
    )

    co_id: Optional[str] = None  # 證券代號
    year: Optional[str] = None  # 民國年（ROC year）
    month: Optional[str] = None  # 月份（mm）
    season: Optional[str] = None  # 季別

    def convert_to_clean_dict(self) -> Dict[str, str]:
        """轉成 dict；值為 None 的欄位視為未指定，不送出"""

        return {key: value for key, value in asdict(self).items() if value is not None}
