import datetime
import importlib
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List

import pandas as pd
import pytest

from core.dao.base import BaseDAO
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.source_priority import dedup_by_source_priority
from core.pipeline.tw.loaders.corporate_action_loader import CorporateActionLoader

"""
`dividend`／`corporate_action` 表 DAO

1. DAO 本身：建表冪等、查詢、`INSERT OR REPLACE` 覆蓋
2. 兩份 `dedup_by_source_priority` 收斂為一份後的語意
3. loader／updater 共用同一個 DAO；寫到一半失敗整批回滾
4. 公司行動偵測改走 DAO 後仍能以已知事件解釋跳空

不連網路、不碰正式的 `tw_stock.db`。
"""


def make_dividend_row(
    date: str, stock_id: str, factor: float = 0.9, source: str = "twse"
) -> Dict[str, object]:
    """建立一列最小可用的 dividend 資料"""

    return {
        "date": date,
        "stock_id": stock_id,
        "證券名稱": f"股票{stock_id}",
        "除權息前收盤價": 100.0,
        "除權息參考價": 100.0 * factor,
        "權息值合計": 100.0 * (1 - factor),
        "權息別": "息",
        "現金股利": 100.0 * (1 - factor),
        "配股率": 0.0,
        "漲停價": None,
        "跌停價": None,
        "開盤競價基準": 100.0 * factor,
        "減除股利參考價": None,
        "還原係數": factor,
        "資料來源": source,
    }


def make_action_row(
    date: str, stock_id: str, ratio: float = 4.0, source: str = "twse"
) -> Dict[str, object]:
    """建立一列最小可用的 corporate_action 資料"""

    return {
        "date": date,
        "stock_id": stock_id,
        "證券名稱": f"股票{stock_id}",
        "停止買賣前收盤價": 100.0,
        "恢復買賣參考價": 100.0 * ratio,
        "調整倍率": ratio,
        "事件類型": "減資",
        "原因": "測試",
        "資料來源": source,
    }


# === DAO ===
def test_dividend_queries() -> None:
    """除權息日去重排序、係數與事件鍵只回指定欄位、表不存在時最新日期為 None"""

    dao: StockDividendDAO = StockDividendDAO(conn=sqlite3.connect(":memory:"))
    assert dao.get_latest_date() is None

    dao.ensure_table()
    dao.ensure_table()
    dao.insert_or_replace(
        pd.DataFrame(
            [
                make_dividend_row("2024-07-02", "2330"),
                make_dividend_row("2024-06-13", "2317"),
                make_dividend_row("2024-07-02", "2317"),
            ]
        )
    )

    assert dao.get_ex_dividend_dates(
        datetime.date(2024, 1, 1), datetime.date(2024, 12, 31)
    ) == [datetime.date(2024, 6, 13), datetime.date(2024, 7, 2)]
    assert list(dao.get_adjust_factors().columns) == ["date", "stock_id", "還原係數"]
    assert list(dao.get_event_keys().columns) == ["date", "stock_id"]
    assert dao.get_by_stock(
        "2317", datetime.date(2024, 1, 1), datetime.date(2024, 12, 31)
    )["date"].tolist() == ["2024-06-13", "2024-07-02"]
    assert dao.get_latest_date() == "2024-07-02"


def test_insert_or_replace_overwrites_same_key() -> None:
    """同鍵再寫一次以新值覆蓋，列數不變（站方更正過的值要進得來）"""

    dao: CorporateActionDAO = CorporateActionDAO(conn=sqlite3.connect(":memory:"))
    dao.ensure_table()

    dao.insert_or_replace(pd.DataFrame([make_action_row("2024-01-02", "2330", 2.0)]))
    dao.insert_or_replace(pd.DataFrame([make_action_row("2024-01-02", "2330", 4.0)]))

    ratios: pd.DataFrame = dao.get_adjust_ratios()
    assert len(ratios) == 1
    assert ratios["調整倍率"].iloc[0] == 4.0
    assert dao.insert_or_replace(pd.DataFrame()) == 0


# === 來源優先序去重 ===
def test_dedup_keeps_highest_priority_regardless_of_order() -> None:
    """優先序最高者勝出，與出現順序無關；結果依鍵排序"""

    df: pd.DataFrame = pd.DataFrame(
        [
            make_action_row("2024-01-03", "2330", 3.0, "twse"),
            make_action_row("2024-01-02", "2330", 1.0, "twse"),
            make_action_row("2024-01-02", "2330", 2.0, "detected"),
        ]
    )

    result: pd.DataFrame = dedup_by_source_priority(
        df, ["detected", "tpex", "twse"], label="test"
    )

    assert result["date"].tolist() == ["2024-01-02", "2024-01-03"]
    assert result["資料來源"].tolist() == ["twse", "twse"]
    assert list(result.columns) == list(df.columns)


def test_dedup_ranks_unknown_source_lowest() -> None:
    """清單中沒列到的來源優先序最低：即使出現在最後也不勝出"""

    df: pd.DataFrame = pd.DataFrame(
        [
            make_action_row("2024-01-02", "2330", 2.0, "detected"),
            make_action_row("2024-01-02", "2330", 9.0, "unknown_source"),
        ]
    )

    result: pd.DataFrame = dedup_by_source_priority(
        df, ["detected", "tpex", "twse"], label="test"
    )

    assert result["資料來源"].tolist() == ["detected"]


def test_dedup_same_priority_keeps_last_occurrence() -> None:
    """同一來源重複時以最後出現的一筆為準（兩份舊實作的共同行為）"""

    df: pd.DataFrame = pd.DataFrame(
        [
            make_action_row("2024-01-02", "2330", 2.0, "twse"),
            make_action_row("2024-01-02", "2330", 5.0, "twse"),
        ]
    )

    result: pd.DataFrame = dedup_by_source_priority(df, ["twse"], label="test")

    assert result["調整倍率"].tolist() == [5.0]


# === loader／updater 共用連線 ===
@pytest.mark.parametrize(
    ("kind", "updater_cls_name", "downloads_attr"),
    [
        ("stock_dividend", "StockDividendUpdater", "DIVIDEND_DOWNLOADS_PATH"),
        (
            "corporate_action",
            "CorporateActionUpdater",
            "CORPORATE_ACTION_DOWNLOADS_PATH",
        ),
    ],
)
def test_updater_and_loader_share_one_connection(
    kind: str,
    updater_cls_name: str,
    downloads_attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """updater 與其 loader 必須是同一條連線，`close()` 後一併關閉"""

    loader_module: ModuleType = importlib.import_module(
        f"core.pipeline.tw.loaders.{kind}_loader"
    )
    updater_module: ModuleType = importlib.import_module(
        f"core.pipeline.tw.updaters.{kind}_updater"
    )

    monkeypatch.setattr(loader_module, downloads_attr, tmp_path / kind)
    monkeypatch.setattr(updater_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))

    # 參數化的 updater 類別只能以名稱取得，型別依 `kind` 而定
    updater: Any = getattr(updater_module, updater_cls_name)()

    assert updater.loader.dao is updater.dao
    assert updater.loader.conn is updater.conn

    updater.close()
    assert updater.dao.conn is None


def test_failed_upsert_leaves_no_partial_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    寫到一半才失敗時整批回滾，例外往外拋，自有連線照樣關閉

    以替身模擬「寫進一部分之後才失敗」（例如磁碟 I/O 錯誤）：先真的寫入，再拋出。
    """

    import core.pipeline.tw.loaders.corporate_action_loader as loader_module

    downloads: Path = tmp_path / "corporate_action"
    downloads.mkdir()
    monkeypatch.setattr(loader_module, "TW_STOCK_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(loader_module, "CORPORATE_ACTION_DOWNLOADS_PATH", downloads)
    pd.DataFrame(
        [make_action_row("2024-01-02", "2330"), make_action_row("2024-01-03", "2317")]
    ).to_csv(downloads / "twse_2024.csv", index=False)

    original_replace: Callable[..., int] = CorporateActionDAO.insert_or_replace

    def replace_then_fail(self: CorporateActionDAO, df: pd.DataFrame) -> int:
        """照常寫入後模擬寫到一半失敗"""

        original_replace(self, df)
        raise OSError("disk I/O error")

    monkeypatch.setattr(CorporateActionDAO, "insert_or_replace", replace_then_fail)

    loader: CorporateActionLoader = loader_module.CorporateActionLoader()
    loader.corporate_action_dir = downloads

    with pytest.raises(OSError):
        loader.add_to_db()

    assert loader.conn is None

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "test.db")
    count: int = conn.execute("SELECT COUNT(*) FROM corporate_action").fetchone()[0]
    conn.close()

    assert count == 0


# === 公司行動偵測 ===
def test_detector_drops_jumps_explained_by_known_events(
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """停牌區間內有已知公司行動的跳空會被解釋掉，沒有事件的留下"""

    from core.pipeline.tw.cleaners.corporate_action_detector import (
        detect_unexplained_moves,
    )

    price_dao: BaseDAO = dao_factory(
        StockPriceDAO,
        records=[
            {"date": "2024-01-02", "stock_id": "2330", "收盤價": 100.0},
            {"date": "2024-01-10", "stock_id": "2330", "收盤價": 400.0},
            {"date": "2024-01-02", "stock_id": "2317", "收盤價": 100.0},
            {"date": "2024-01-03", "stock_id": "2317", "收盤價": 50.0},
        ],
    )
    conn: sqlite3.Connection = price_dao.conn

    action_dao: CorporateActionDAO = CorporateActionDAO(conn=conn)
    action_dao.ensure_table()
    action_dao.insert_or_replace(pd.DataFrame([make_action_row("2024-01-08", "2330")]))
    action_dao.commit()

    result: pd.DataFrame = detect_unexplained_moves(conn=conn)

    remaining: List[str] = result["stock_id"].tolist()
    assert remaining == ["2317"]
