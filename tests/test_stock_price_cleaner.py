import datetime
from pathlib import Path

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.stock_price_cleaner import StockPriceCleaner

"""
台股收盤行情清洗測試

**本檔盯的是「正負號存在另一欄」這件事**：TWSE 的 `漲跌價差` 是絕對值，
正負號只在 `漲跌(+/-)` 欄（`+`／`-`／`X`，`X` 代表除權息、價差一律為 0）。
把符號欄直接丟掉的話，下跌日在庫裡會變成上漲，而且完全不會報錯——
實測 2330 在 2025-04-07 收 848（前一日 942），庫內 `漲跌價差` 是 +94.0。
上櫃的 `漲跌價差` 則自帶正負號，兩邊若不統一，同一張表的上市、上櫃語意不同。

不連網路、不碰正式 DB。
"""

DATE: datetime.date = datetime.date(2025, 4, 7)


@pytest.fixture
def cleaner(tmp_path: Path) -> StockPriceCleaner:
    """清洗結果落地改寫到暫存目錄"""

    price_cleaner: StockPriceCleaner = StockPriceCleaner()
    price_cleaner.price_dir = tmp_path
    return price_cleaner


def twse_raw() -> pd.DataFrame:
    """TWSE 版面：下跌、上漲與除權息各一列"""

    return pd.DataFrame(
        [
            {
                "證券代號": "2330",
                "證券名稱": "台積電",
                "成交股數": 37_652_704,
                "成交筆數": 219_438,
                "成交金額": 32_012_146_992,
                "開盤價": "848.00",
                "最高價": "848.00",
                "最低價": "848.00",
                "收盤價": "848.00",
                "漲跌(+/-)": "-",
                "漲跌價差": 94.0,
                "最後揭示買價": "--",
                "最後揭示買量": 0,
                "最後揭示賣價": "848.00",
                "最後揭示賣量": 118_510,
                "本益比": 18.74,
            },
            {
                "證券代號": "00632R",
                "證券名稱": "元大台灣50反1",
                "成交股數": 1_000_000,
                "成交筆數": 500,
                "成交金額": 27_230_000,
                "開盤價": "27.00",
                "最高價": "27.30",
                "最低價": "26.90",
                "收盤價": "27.23",
                "漲跌(+/-)": "+",
                "漲跌價差": 2.47,
                "最後揭示買價": "27.22",
                "最後揭示買量": 100,
                "最後揭示賣價": "27.23",
                "最後揭示賣量": 200,
                "本益比": 0.0,
            },
            {
                "證券代號": "1414",
                "證券名稱": "東和",
                "成交股數": 100_000,
                "成交筆數": 50,
                "成交金額": 1_820_000,
                "開盤價": "18.20",
                "最高價": "18.20",
                "最低價": "18.20",
                "收盤價": "18.20",
                "漲跌(+/-)": "X",
                "漲跌價差": 0.0,
                "最後揭示買價": "18.15",
                "最後揭示買量": 100,
                "最後揭示賣價": "18.20",
                "最後揭示賣量": 100,
                "本益比": 0.0,
            },
        ]
    )


def test_down_day_keeps_the_minus_sign(cleaner: StockPriceCleaner) -> None:
    """下跌日的 `漲跌價差` 必須是負數"""

    cleaned: pd.DataFrame = cleaner.clean_twse_price(twse_raw(), DATE).set_index(
        "stock_id"
    )

    assert cleaned.loc["2330", "漲跌價差"] == pytest.approx(-94.0)


def test_up_day_stays_positive(cleaner: StockPriceCleaner) -> None:
    """上漲日不受影響（防止改過頭把整欄變成負的）"""

    cleaned: pd.DataFrame = cleaner.clean_twse_price(twse_raw(), DATE).set_index(
        "stock_id"
    )

    assert cleaned.loc["00632R", "漲跌價差"] == pytest.approx(2.47)


def test_ex_dividend_row_is_zero(cleaner: StockPriceCleaner) -> None:
    """除權息（`X`）那一列的價差是 0，符號不適用"""

    cleaned: pd.DataFrame = cleaner.clean_twse_price(twse_raw(), DATE).set_index(
        "stock_id"
    )

    assert cleaned.loc["1414", "漲跌價差"] == pytest.approx(0.0)
    assert "漲跌(+/-)" not in cleaned.columns
