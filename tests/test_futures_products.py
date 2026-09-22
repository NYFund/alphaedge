import datetime
import sqlite3
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
from core.backtest.models.cost_model import TwFuturesCostModel
from core.config import FUTURES_TARGET_PRODUCTS, TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.managers.futures.position_manager import FuturesPositionManager
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.models import FuturesAccount, FuturesOrder, FuturesQuote
from core.models.cost_config import FuturesCostConfig
from core.utils import Action, PositionType, Scale
from core.utils.constant import FUTURES_MULTIPLIER

"""
多商品擴充測試

**每個商品的乘數不同，而乘數錯了不會報錯**——只會讓整條 PnL 靜默偏掉：
TX 一點 200 元、MTX 50 元、TMF 10 元、TE 4,000 元、TF 1,000 元。
同樣漲 100 點，TX 賺 20,000、TMF 只賺 1,000，差 20 倍。

本檔釘住三件事：

1. **收錄門檻是「乘數已查證」**：`FUTURES_TARGET_PRODUCTS` 內的每一檔都必須
   在 `FUTURES_MULTIPLIER` 有登錄，否則爬回來也算不出 PnL，會拖到回測才 KeyError。
2. **乘數逐商品套用**，不是全場共用一個。
3. **契約價值與保證金也跟著乘數走**，不可沿用大台的數字。
"""

DATE: datetime.date = datetime.date(2026, 8, 28)


def make_price_df(product: str, close: float) -> pd.DataFrame:
    """組出單商品單契約的行情表"""

    return pd.DataFrame(
        [
            {
                "date": str(DATE),
                "product": product,
                "expiry": "202609",
                "session": "day",
                FuturesPriceColumn.OPEN.value: close,
                FuturesPriceColumn.HIGH.value: close,
                FuturesPriceColumn.LOW.value: close,
                FuturesPriceColumn.CLOSE.value: close,
                FuturesPriceColumn.VOLUME.value: 100,
                FuturesPriceColumn.SETTLEMENT.value: close,
                FuturesPriceColumn.OPEN_INTEREST.value: 50,
            }
        ]
    )


# === 收錄門檻 ===
def test_every_target_product_has_a_verified_multiplier() -> None:
    """
    **乘數未查證的商品不可進 `FUTURES_TARGET_PRODUCTS`**

    爬回來也算不出 PnL，而且會拖到回測階段才 KeyError——那時已經很難聯想到
    是「當初多加了一檔」造成的。
    """

    missing: List[str] = [
        product
        for product in FUTURES_TARGET_PRODUCTS
        if product not in FUTURES_MULTIPLIER
    ]

    assert not missing, f"這些商品的乘數尚未查證登錄：{missing}"


def test_target_products_cover_the_index_futures_family() -> None:
    """擴充清單：大台 ＋ 小台 ＋ 微台 ＋ 電子 ＋ 金融（含小型）"""

    assert set(FUTURES_TARGET_PRODUCTS) == {
        "TX",
        "MTX",
        "TMF",
        "TE",
        "ZEF",
        "TF",
        "ZFF",
    }


# === 乘數逐商品套用 ===
@pytest.mark.parametrize(
    "product, multiplier",
    [("TX", 200), ("MTX", 50), ("TMF", 10), ("TE", 4000), ("TF", 1000)],
)
def test_adapter_applies_the_product_multiplier(product: str, multiplier: int) -> None:
    """報價的乘數取自 `FUTURES_MULTIPLIER`，逐商品不同"""

    quotes: List[FuturesQuote] = FuturesQuoteAdapter.generate_futures_quotes(
        make_price_df(product, 20000.0), DATE, Scale.DAY
    )

    assert len(quotes) == 1
    assert quotes[0].multiplier == multiplier


@pytest.mark.parametrize(
    "product, expected_pnl",
    [("TX", 20000), ("MTX", 5000), ("TMF", 1000), ("TE", 400000), ("TF", 100000)],
)
def test_same_point_move_gives_different_pnl(product: str, expected_pnl: int) -> None:
    """
    **同樣漲 100 點，不同商品的損益差到 400 倍**

    這正是乘數不可共用的理由：TX 與 TMF 的行情走勢完全相同，賺的錢差 20 倍。
    """

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=50_000_000),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )

    pnl: float = manager.calculate_pnl(
        position_type=PositionType.LONG,
        entry_price=20000.0,
        exit_price=20100.0,
        volume=1,
        multiplier=FUTURES_MULTIPLIER[product],
    )

    assert pnl == expected_pnl


def test_margin_follows_the_contract_value_per_product() -> None:
    """比率模式的保證金 ＝ 契約價值 × 比率，故也隨乘數而不同"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=50_000_000),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(0.1),
    )

    tx: float = manager.calculate_margin(20000.0, 1, FUTURES_MULTIPLIER["TX"])
    mtx: float = manager.calculate_margin(20000.0, 1, FUTURES_MULTIPLIER["MTX"])

    assert tx == 20000 * 200 * 0.1
    assert mtx == 20000 * 50 * 0.1
    assert tx == mtx * 4  # 大台是小台的四倍


def test_open_position_looks_up_the_multiplier_by_product() -> None:
    """開倉時的乘數由**訂單的商品**決定，不是由設定或預設值決定"""

    manager: FuturesPositionManager = FuturesPositionManager(
        FuturesAccount(init_capital=50_000_000),
        cost_model=TwFuturesCostModel(FuturesCostConfig.free()),
        margin_config=FuturesMarginConfig.ratio(),
    )

    position = manager.open_position(
        FuturesOrder(
            product="TMF",
            expiry="202609",
            date=DATE,
            action=Action.BUY,
            position_type=PositionType.LONG,
            price=20000.0,
            volume=1,
        )
    )

    assert position.multiplier == FUTURES_MULTIPLIER["TMF"] == 10


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗多商品"
)
def test_every_product_in_the_table_has_a_multiplier_source() -> None:
    """
    **表內每一檔商品都要有「乘數的出處」**，而出處有兩個且不可混用：

    | 類型 | 乘數來源 | 為什麼 |
    |------|----------|--------|
    | 指數期貨（TX、MTX…） | `FUTURES_MULTIPLIER` 常數 | 固定不變，寫在程式碼裡 |
    | 股票期貨（CDF、NYF…） | `futures_stock_universe.contract_size` | **會因除權息調整**，寫死必錯 |

    回補新商品時若兩邊都查不到，本測試會在資料進表的那一刻就失敗，
    而不是等到有人拿它回測才 KeyError。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        products: List[str] = [
            row[0]
            for row in conn.execute("SELECT DISTINCT product FROM futures_price_daily")
        ]
        stock_futures: set = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT product_id FROM futures_stock_universe"
            )
        }
    finally:
        conn.close()

    orphans: List[str] = [
        product
        for product in products
        if product not in FUTURES_MULTIPLIER and product not in stock_futures
    ]

    assert not orphans, (
        f"這些商品在表裡有行情，但乘數兩邊都查不到：{orphans}"
        f"（指數期貨請登錄 FUTURES_MULTIPLIER，股期請確認標的池已更新）"
    )
    assert "TX" in products
