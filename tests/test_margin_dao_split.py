import datetime
import sqlite3
import warnings
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.dao.tw.financial_statement_dao import FinancialStatementDAO
from core.dao.tw.futures_chip_dao import FuturesChipDAO
from core.dao.tw.futures_margin_dao import FuturesMarginDAO
from core.dao.tw.monthly_revenue_dao import MonthlyRevenueDAO
from core.dao.tw.stock_futures_margin_rate_dao import StockFuturesMarginRateDAO
from core.pipeline.tw.loaders.futures_margin_loader import FuturesMarginLoader

"""
多表 DAO 收斂：保證金兩張表各一個 DAO、動態 schema 的建表方法與基底分開命名

原本 `FuturesMarginDAO` 一個 instance 管兩張表、靠 `table` 參數切換，每個方法都得先驗表名；
`ensure_table` 有 `()`、`(df)`、`(columns)` 三種簽名，以基底型別持有 DAO 的呼叫端無從分辨。
"""

RATE_COLUMNS: List[str] = [
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


def make_rates(rows: List[list]) -> pd.DataFrame:
    """組比例表的資料；第一欄是 `datetime.date`"""

    return pd.DataFrame(rows, columns=RATE_COLUMNS)


@pytest.fixture
def rate_dao() -> StockFuturesMarginRateDAO:
    """CDF 只有一覽表那一列（級距穩定的商品從不出現在公告裡）"""

    dao: StockFuturesMarginRateDAO = StockFuturesMarginRateDAO(
        conn=sqlite3.connect(":memory:")
    )
    dao.ensure_table()
    dao.insert_rows(
        make_rates(
            [
                [
                    datetime.date(2024, 8, 9),
                    "CDF",
                    "2330",
                    "台積電期貨",
                    "級距1",
                    0.1035,
                    0.115,
                    0.135,
                    "snapshot",
                ],
                [
                    datetime.date(2024, 8, 9),
                    "DHF",
                    "2454",
                    "聯發科期貨",
                    None,
                    0.138,
                    0.153,
                    0.18,
                    "snapshot",
                ],
            ]
        )
    )
    dao.commit()
    return dao


def test_rate_fallback_to_earliest_is_opt_in(
    rate_dao: StockFuturesMarginRateDAO,
) -> None:
    """查詢日早於表內所有列：預設回 None，明確要求時才退回最早一列"""

    early: datetime.date = datetime.date(2020, 1, 1)

    assert rate_dao.get_rates_in_effect("CDF", early) is None
    assert rate_dao.get_rates_in_effect("CDF", early, fallback_to_earliest=True) == (
        0.1035,
        0.115,
        0.135,
    )
    assert rate_dao.get_rates_in_effect("CDF", datetime.date(2024, 9, 1)) == (
        0.1035,
        0.115,
        0.135,
    )


def test_effective_date_is_written_as_iso_text_without_the_deprecated_adapter(
    rate_dao: StockFuturesMarginRateDAO,
) -> None:
    """生效日由 DAO 自己轉成 ISO 字串：不靠 Python 3.12 已 deprecated 的預設 date adapter"""

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        rate_dao.insert_rows(
            make_rates(
                [
                    [
                        datetime.date(2024, 9, 1),
                        "CDF",
                        "2330",
                        "台積電期貨",
                        "級距1",
                        0.11,
                        0.12,
                        0.15,
                        "announcement",
                    ]
                ]
            )
        )

    stored = rate_dao.conn.execute(
        f"SELECT effective_date, typeof(effective_date) FROM {rate_dao.TABLE_NAME} "
        "WHERE product_id = 'CDF' ORDER BY effective_date DESC LIMIT 1"
    ).fetchone()
    assert stored == ("2024-09-01", "text")


def test_summary_counts_rows_and_products(rate_dao: StockFuturesMarginRateDAO) -> None:
    """摘要的（表名, 列數, 商品數, 最早, 最晚）要真的算商品數"""

    assert rate_dao.get_summary() == (
        rate_dao.TABLE_NAME,
        2,
        2,
        "2024-08-09",
        "2024-08-09",
    )

    amount: FuturesMarginDAO = FuturesMarginDAO(conn=rate_dao.conn)
    amount.ensure_table()
    assert amount.get_summary() == (amount.TABLE_NAME, 0, 0, None, None)


def test_loader_routes_each_table_to_its_own_dao(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """loader 以表名分流到對應的 DAO；不是兩張保證金表之一當場 `ValueError`"""

    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.TW_FUTURES_DB_PATH",
        tmp_path / "tw_futures.db",
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_margin_loader.FUTURES_MARGIN_DOWNLOADS_PATH",
        tmp_path / "margin",
    )
    loader: FuturesMarginLoader = FuturesMarginLoader()

    assert loader.dao_for(FuturesMarginDAO.TABLE_NAME) is loader.dao
    assert loader.dao_for(StockFuturesMarginRateDAO.TABLE_NAME) is loader.rate_dao
    assert loader.rate_dao.conn is loader.dao.conn
    with pytest.raises(ValueError):
        loader.dao_for("price")
    with pytest.raises(ValueError):
        loader.count_rows("price; DROP TABLE x")

    loader.disconnect()
    assert loader.rate_dao is None


@pytest.mark.parametrize(
    "make_dao",
    [
        lambda conn: FuturesChipDAO("futures_large_trader", conn=conn),
        lambda conn: MonthlyRevenueDAO(conn=conn),
        lambda conn: FinancialStatementDAO("balance_sheet", conn=conn),
    ],
    ids=["chip", "monthly_revenue", "financial_statement"],
)
def test_dynamic_schema_daos_refuse_the_argument_free_builder(make_dao) -> None:
    """欄位由資料或清單決定的表：無參數的 `ensure_table()` 明確拋出，而不是靜默不建表"""

    dao = make_dao(sqlite3.connect(":memory:"))

    with pytest.raises(NotImplementedError, match="ensure_table_from_columns"):
        dao.ensure_table()
    assert not dao.table_exists()
