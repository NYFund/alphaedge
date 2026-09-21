from pathlib import Path

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.finmind_cleaner import FinMindCleaner

"""
FinMind 台股總覽清洗：同一檔多列時保留最新的身分

FinMind 每個身分各給一列，興櫃轉上市櫃的公司同時有過期的 `emerging` 列與今天的
`twse`／`tpex` 列，而且舊的排在前面。留錯列的話，以 `type` 取上市櫃清單的財報
權益變動表就把這些公司排除（2026-09-21 實測 154 檔還在交易的公司被標成興櫃）。
"""


@pytest.fixture
def cleaner(tmp_path: Path) -> FinMindCleaner:
    instance: FinMindCleaner = FinMindCleaner.__new__(FinMindCleaner)
    instance.finmind_dir = tmp_path
    return instance


def raw_info() -> pd.DataFrame:
    """與 2026-09-21 實際回傳同形狀：過期的興櫃列在前、同日兩個產業別"""

    return pd.DataFrame(
        [
            ["食品工業", "1294", "漢田生技", "emerging", "2024-09-25"],
            ["半導體業", "2330", "台積電", "twse", "2026-09-22"],
            ["電子工業", "2330", "台積電", "twse", "2026-09-22"],
            ["食品工業", "1294", "漢田生技", "tpex", "2026-09-22"],
        ],
        columns=["industry_category", "stock_id", "stock_name", "type", "date"],
    )


@pytest.mark.parametrize(
    "method", ["clean_stock_info", "clean_stock_info_with_warrant"]
)
def test_latest_status_wins(cleaner: FinMindCleaner, method: str) -> None:
    """轉板的公司留今天的身分；同日多列留原本排在前面的"""

    cleaned: pd.DataFrame = getattr(cleaner, method)(raw_info())
    rows = cleaned.set_index("stock_id")

    assert len(cleaned) == 2
    assert rows.loc["1294", "type"] == "tpex"
    assert rows.loc["2330", "industry_category"] == "半導體業"
