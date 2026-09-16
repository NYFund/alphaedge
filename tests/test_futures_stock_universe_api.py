import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pytest

from core.api.tw.futures_stock_universe_api import FuturesStockUniverseAPI
from core.config import TW_FUTURES_DB_PATH

"""
股票期貨標的池 API 與乘數測試

**股期與指數期貨最根本的差異：乘數不是常數**。指數期貨的乘數寫在
`FUTURES_MULTIPLIER`（TX 200）幾十年不變；股期的「契約單位」標準型是 2,000 股，
但**除權息之後會被交易所調整**，調整後的契約甚至換一個代碼。拿今天的契約單位
回測歷史，除權息之後那一段的 PnL 會整段偏掉，而且不會有任何錯誤。

**與台股還原價的關係——最容易雙重調整的地方**：台股用「還原價」處理除權息
（價格往回調），股期用「調整契約單位」處理（價格不動、每口股數變）。
兩者是同一件事的兩種做法，**擇一即可**；同時套用就是雙重調整。
本專案的規則是股期行情一律用原始價（`adj_close` 恆為 None）。
"""


@pytest.fixture
def universe_api() -> FuturesStockUniverseAPI:
    """建一個含兩份快照的記憶體標的池（模擬除權息後契約單位被調整）"""

    conn: sqlite3.Connection = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE futures_stock_universe (
            snapshot_date TEXT NOT NULL,
            product_id TEXT NOT NULL,
            base_code TEXT,
            product_type TEXT,
            underlying_stock_id TEXT,
            underlying_name TEXT,
            underlying_listing_board TEXT,
            contract_size INT,
            day_session_time TEXT,
            night_session_time TEXT,
            PRIMARY KEY (snapshot_date, product_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE futures_price_daily (
            date TEXT, product TEXT, expiry TEXT, session TEXT,
            開盤價 REAL, 最高價 REAL, 最低價 REAL, 收盤價 REAL,
            成交量 INT, 結算價 REAL, 未沖銷契約量 INT
        )
        """
    )
    rows = [
        (
            "2026-08-01",
            "CDF",
            "CD",
            "個股期貨",
            "2330",
            "台積電",
            "上市",
            2000,
            "",
            None,
        ),
        (
            "2026-08-01",
            "NYF",
            "NY",
            "ETF期貨",
            "0050",
            "元大台灣50",
            "上市",
            10000,
            "",
            None,
        ),
        (
            "2026-08-01",
            "XYF",
            "XY",
            "小型個股期貨",
            "2603",
            "長榮",
            "上市",
            100,
            "",
            None,
        ),
        # 除權息後契約單位被調整（2,000 → 2,150）
        (
            "2026-08-29",
            "CDF",
            "CD",
            "個股期貨",
            "2330",
            "台積電",
            "上市",
            2150,
            "",
            None,
        ),
        (
            "2026-08-29",
            "NYF",
            "NY",
            "ETF期貨",
            "0050",
            "元大台灣50",
            "上市",
            10000,
            "",
            None,
        ),
        (
            "2026-08-29",
            "XYF",
            "XY",
            "小型個股期貨",
            "2603",
            "長榮",
            "上市",
            100,
            "",
            None,
        ),
    ]
    conn.executemany(
        "INSERT INTO futures_stock_universe VALUES (?,?,?,?,?,?,?,?,?,?)", rows
    )
    prices = [
        # CDF 天天有量、NYF 量少、XYF 只有一天（樣本不足）
        *[
            (f"2026-08-{day:02d}", "CDF", "202609", "day", 1, 1, 1, 1, 5000, 1, 1)
            for day in range(4, 12)
        ],
        *[
            (f"2026-08-{day:02d}", "NYF", "202609", "day", 1, 1, 1, 1, 100, 1, 1)
            for day in range(4, 12)
        ],
        ("2026-08-04", "XYF", "202609", "day", 1, 1, 1, 1, 999999, 1, 1),
    ]
    conn.executemany(
        "INSERT INTO futures_price_daily VALUES (?,?,?,?,?,?,?,?,?,?,?)", prices
    )
    conn.commit()

    return FuturesStockUniverseAPI(conn=conn)


# === 快照語意 ===
def test_snapshot_is_resolved_by_date(universe_api) -> None:
    """本表是快照序列，任何「某日的狀態」都要先解出該日適用的快照"""

    assert universe_api.get_snapshot_date(datetime.date(2026, 8, 15)) == "2026-08-01"
    assert universe_api.get_snapshot_date(datetime.date(2026, 9, 1)) == "2026-08-29"
    assert universe_api.get_snapshot_date() == "2026-08-29"


def test_query_before_the_first_snapshot_falls_back(universe_api) -> None:
    """
    查詢日早於第一份快照時退回最早的一份

    **這是近似不是事實**：本表只回溯到建表之日，更早的掛牌狀態無從得知。
    回 None 會讓所有早期回測直接沒有商品清單，退回最早一份至少可跑，
    但已在 API docstring 標明其限制。
    """

    assert universe_api.get_snapshot_date(datetime.date(2020, 1, 1)) == "2026-08-01"


# === 乘數（契約單位）===
def test_contract_size_follows_the_snapshot_date(universe_api) -> None:
    """
    **除權息調整後的契約單位要查當時的快照**

    拿今天的 2,150 去算除權息之前的損益，每口會多算 150 股。
    """

    assert universe_api.get_contract_size("CDF", datetime.date(2026, 8, 15)) == 2000
    assert universe_api.get_contract_size("CDF", datetime.date(2026, 9, 1)) == 2150


def test_contract_size_differs_by_product_type(universe_api) -> None:
    """標準型 2,000 股、小型 100 股、ETF 10,000 股——**不可用預設值代替**"""

    assert universe_api.get_contract_size("NYF") == 10000
    assert universe_api.get_contract_size("XYF") == 100
    assert universe_api.get_contract_size("NOT_EXIST") is None


def test_contract_size_history_only_lists_changes(universe_api) -> None:
    """
    契約單位的變動序列由快照差分推得——來源沒有「調整生效日」這個欄位

    沒變動的商品只會有一列（第一份快照）。
    """

    changed: pd.DataFrame = universe_api.get_contract_size_history("CDF")
    unchanged: pd.DataFrame = universe_api.get_contract_size_history("NYF")

    assert list(changed["contract_size"]) == [2000, 2150]
    assert list(changed["snapshot_date"]) == ["2026-08-01", "2026-08-29"]
    assert len(unchanged) == 1


def test_underlying_links_back_to_the_stock(universe_api) -> None:
    """要與 `tw_stock.db` 對照（例如比對除權息）就得知道標的證券"""

    underlying = universe_api.get_underlying("CDF")

    assert underlying["underlying_stock_id"] == "2330"
    assert underlying["product_type"] == "個股期貨"


# === 流動性篩選 ===
def test_top_liquid_products_are_ranked_by_average_volume(universe_api) -> None:
    """
    依**平均日成交量**排序

    股期的流動性差距是數量級的；把尾端商品納入回測只會製造
    「回測賺錢、實際掛不到單」的假訊號。
    """

    assert universe_api.get_top_liquid_products(2, min_days=5) == ["CDF", "NYF"]


def test_short_lived_products_are_excluded(universe_api) -> None:
    """
    **只上市兩天就爆量的商品不能代表長期流動性**

    XYF 單日 999,999 口遠高於 CDF，但只有一天資料，`min_days` 應把它排除。
    """

    assert "XYF" not in universe_api.get_top_liquid_products(5, min_days=5)
    # 放寬門檻後它才會出現（且排第一）
    assert universe_api.get_top_liquid_products(1, min_days=1) == ["XYF"]


# === 與台股除權息處理的分工 ===
def test_stock_futures_quotes_are_never_price_adjusted() -> None:
    """
    **股期行情一律用原始價，不套還原係數**

    股期的除權息由「調整契約單位」承接；再套台股的還原價就是雙重調整。
    """

    from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter
    from core.config.schema import FuturesPriceColumn
    from core.utils import Scale

    df: pd.DataFrame = pd.DataFrame(
        [
            {
                "date": "2026-08-28",
                "product": "CDF",
                "expiry": "202609",
                "session": "day",
                FuturesPriceColumn.OPEN.value: 2432,
                FuturesPriceColumn.HIGH.value: 2439,
                FuturesPriceColumn.LOW.value: 2417,
                FuturesPriceColumn.CLOSE.value: 2419,
                FuturesPriceColumn.VOLUME.value: 4118,
                FuturesPriceColumn.SETTLEMENT.value: 2419,
                FuturesPriceColumn.OPEN_INTEREST.value: 25832,
            }
        ]
    )

    quotes = FuturesQuoteAdapter.generate_futures_quotes(
        df, datetime.date(2026, 8, 28), Scale.DAY, multiplier_resolver=lambda _: 2000
    )

    assert quotes[0].adj_close is None
    assert quotes[0].close == 2419
    assert quotes[0].multiplier == 2000


def test_multiplier_resolver_is_required_for_stock_futures() -> None:
    """
    **股期不在 `FUTURES_MULTIPLIER` 裡**，沒有解析器就該 KeyError

    靜默套一個預設乘數會讓整條 PnL 偏掉；中斷比靜默錯誤好查。
    """

    from core.adapters.tw.futures_quote_adapter import FuturesQuoteAdapter

    with pytest.raises(KeyError):
        FuturesQuoteAdapter.resolve_multiplier("CDF")


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗標的池"
)
def test_real_universe_covers_the_stock_futures_in_the_price_table() -> None:
    """
    表內每一檔股期行情都要在標的池查得到契約單位

    查不到就代表那一檔的 PnL 算不出來——回測時才發現太晚。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        products: List[str] = [
            row[0]
            for row in conn.execute("SELECT DISTINCT product FROM futures_price_daily")
        ]
    finally:
        conn.close()

    from core.utils.constant import FUTURES_MULTIPLIER

    api: FuturesStockUniverseAPI = FuturesStockUniverseAPI()
    try:
        missing: List[str] = []
        for product in products:
            if product in FUTURES_MULTIPLIER:
                continue
            size: Optional[int] = api.get_contract_size(product)
            if size is None:
                missing.append(product)
    finally:
        api.close()

    assert not missing, f"這些股期在標的池查不到契約單位：{missing}"
