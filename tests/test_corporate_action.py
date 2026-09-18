import datetime
import sqlite3
from pathlib import Path
from typing import Callable, Dict, Iterator, List

import pandas as pd
import pytest

from core.config import CORPORATE_ACTION_TABLE_NAME
from core.dao.base import BaseDAO
from core.dao.tw.stock_price_dao import StockPriceDAO
from core.pipeline.shared.source_priority import dedup_by_source_priority
from core.pipeline.tw.cleaners.corporate_action_cleaner import (
    OUTPUT_COLUMNS,
    CorporateActionCleaner,
)
from core.pipeline.tw.loaders.corporate_action_loader import CorporateActionLoader
from core.pipeline.utils.exceptions import ColumnLayoutError

"""
非除權息公司行動 ETL（還原價 S2）

`dividend` 表只記除權息，**所有不走除權息流程的公司行動都不在裡面**。實測
2013~2026 上市 360 筆減資事件、涵蓋 243 檔，沒有一筆出現在 `dividend`；
最極端的南亞科 2014-09-09 是 8.10 → 80.93（9.99×）。少了它們，還原價在那天
就是一個假跳空。

本檔以站方**實際回傳過的資料**為樣本，全部離線、不連網路也不碰 production DB。
"""


# TWSE 的真實回傳格式（`response=json` 的 fields ＋ data）
TWSE_FIELDS: List[str] = [
    "恢復買賣日期",
    "股票代號",
    "名稱",
    "停止買賣前收盤價格",
    "恢復買賣參考價",
    "漲停價格",
    "跌停價格",
    "開盤競價基準",
    "除權參考價",
    "減資原因",
    "詳細資料",
]

TPEX_FIELDS: List[str] = [
    "恢復買賣日期",
    "股票代號",
    "名稱",
    "最後交易日之收盤價格",
    "減資恢復買賣開始日參考價格",
    "漲停價格",
    "跌停價格",
    "開始交易基準價",
    "除權參考價",
    "減資原因",
    "詳細資料",
]


def tpex_detail(shares: float, refund: float) -> str:
    """組出 TPEX `詳細資料` 欄的 HTML（只保留本測試用得到的兩項）"""

    return (
        "<table>"
        f"<tr><th>每壹仟股換發新股票:</th><td>{shares:.8f}&nbsp股</td></tr>"
        f"<tr><th>每股退還股款:</th><td>{refund:.8f}&nbsp元/股</td></tr>"
        "</table>"
    )


@pytest.fixture
def cleaner() -> CorporateActionCleaner:
    return CorporateActionCleaner()


# === 日期解析：兩邊格式不同 ===
def test_parse_roc_date_handles_both_formats() -> None:
    """
    TWSE 給 `114/02/12`、TPEX 給 `1140113`，是同一個欄位的兩種寫法

    分成兩支函式只會讓呼叫端要記得自己在哪一邊。
    """

    parse = CorporateActionCleaner.parse_roc_date

    assert parse("114/02/12") == datetime.date(2025, 2, 12)
    assert parse("1140113") == datetime.date(2025, 1, 13)
    assert parse("103/09/09") == datetime.date(2014, 9, 9)
    assert parse("") is None
    assert parse("--") is None


# === 倍率與 TWSE 清洗 ===
def test_twse_cleaning_computes_ratio(cleaner: CorporateActionCleaner) -> None:
    """
    南亞科 2014-09-09：8.10 → 80.93，倍率 9.99

    這是全期間最極端的一筆，也是驗收指定的案例。
    """

    raw: pd.DataFrame = pd.DataFrame(
        [
            [
                "103/09/09",
                "2408",
                "南亞科",
                "8.10",
                "80.93",
                "86.50",
                "72.90",
                "80.90",
                "--",
                "彌補虧損",
                "",
            ]
        ],
        columns=TWSE_FIELDS,
    )

    cleaned: pd.DataFrame = cleaner.clean(raw, source="twse")

    assert list(cleaned.columns) == OUTPUT_COLUMNS
    row = cleaned.iloc[0]
    assert row["date"] == datetime.date(2014, 9, 9)
    assert row["stock_id"] == "2408"
    assert row["調整倍率"] == pytest.approx(9.9914, abs=1e-4)
    assert row["事件類型"] == "減資"
    assert row["資料來源"] == "twse"


def test_ratio_direction_is_opposite_to_dividend(
    cleaner: CorporateActionCleaner,
) -> None:
    """
    減資的倍率 **> 1**（價格上調），與 `dividend.還原係數`（恆 < 1）方向相反

    這正是本表不能併進 `dividend` 的原因：塞進同一欄會讓所有既有讀取端的
    假設反向。
    """

    raw: pd.DataFrame = pd.DataFrame(
        [
            [
                "114/06/23",
                "2371",
                "大同",
                "40.15",
                "41.73",
                "",
                "",
                "",
                "",
                "退還股款",
                "",
            ]
        ],
        columns=TWSE_FIELDS,
    )

    assert cleaner.clean(raw, source="twse").iloc[0]["調整倍率"] > 1


# === 分類：這是我實作時抓到的錯 ===
@pytest.mark.parametrize(
    ("reason", "ratio"),
    [("退還股款", 0.9220), ("現金減資", 0.9495), ("彌補虧損", 0.9902)],
)
def test_endpoint_rows_are_never_classified_as_split(reason: str, ratio: float) -> None:
    """
    端點來源即使倍率 < 1 也不是分割

    ⚠️ **不可用倍率方向判斷分割**：這兩個端點本身就是**減資**端點，分割不會
    出現在裡面。而「退還股款」型減資的參考價可能**低於**前收盤價——退還的現金
    比股數合併的效果還大時就會這樣。實測 632 筆裡有 7 筆（協禧 0.922、
    無敵 0.934、中環 0.950…）原因欄明寫減資，卻因倍率 < 1 被誤判成分割。
    """

    classify = CorporateActionCleaner.classify_event

    assert classify(reason=reason, ratio=ratio, source="twse") == "減資"
    assert classify(reason=reason, ratio=ratio, source="tpex") == "減資"


def test_detected_rows_use_ratio_direction() -> None:
    """`detected` 來源沒有原因欄可用，只能靠倍率方向分辨分割與反向分割"""

    classify = CorporateActionCleaner.classify_event

    assert classify(reason="股票分割 1拆4", ratio=0.25, source="detected") == "分割"
    assert classify(reason="反向分割", ratio=7.0, source="detected") == "反向分割"


def test_par_value_change_wins_over_ratio() -> None:
    """原因寫明面額變更時以它為準，不看倍率"""

    assert (
        CorporateActionCleaner.classify_event(
            reason="面額變更", ratio=0.1, source="twse"
        )
        == "面額變更"
    )


# === 站方會給重複列 ===
def test_dedup_keeps_one_row_per_key(cleaner: CorporateActionCleaner) -> None:
    """
    公司改名期間同一筆事件會出現兩次，價格相同、名稱不同

    實測南亞科 2014-09-09 以「南亞科」與「南科」各出現一次。主鍵是
    `(date, stock_id)`，不先去重就會在入庫時撞鍵。
    """

    raw: pd.DataFrame = pd.DataFrame(
        [
            [
                "103/09/09",
                "2408",
                "南亞科",
                "8.10",
                "80.93",
                "",
                "",
                "",
                "",
                "彌補虧損",
                "",
            ],
            [
                "103/09/09",
                "2408",
                "南科",
                "8.10",
                "80.93",
                "",
                "",
                "",
                "",
                "彌補虧損",
                "",
            ],
        ],
        columns=TWSE_FIELDS,
    )

    cleaned: pd.DataFrame = cleaner.clean(raw, source="twse")

    assert len(cleaned) == 1


# === TPEX 的交叉驗證公式 ===
def test_tpex_cross_check_uses_cash_refund(
    cleaner: CorporateActionCleaner, caplog
) -> None:
    """
    站方公式是 `(前收 − 每股退還股款) × 1000 ÷ 換發股數`

    ⚠️ 退還股款那一項不能省。只用換股比例在「彌補虧損」型會剛好對上，
    但「退還股款」型會整批對不上——實測 3290 東浦（前收 28.65、退還 3.00、
    換發 700 股）用純換股比例推得 40.93，與站方的 36.64 差很多；
    補上退還股款後分毫不差。
    """

    raw: pd.DataFrame = pd.DataFrame(
        [
            [
                "1140825",
                "3290",
                "東浦",
                "28.65",
                "36.64",
                "",
                "",
                "",
                "",
                "退還股款",
                tpex_detail(shares=700.0, refund=3.0),
            ]
        ],
        columns=TPEX_FIELDS,
    )

    with caplog.at_level("WARNING"):
        cleaned: pd.DataFrame = cleaner.clean(raw, source="tpex")

    assert cleaned.iloc[0]["調整倍率"] == pytest.approx(36.64 / 28.65, abs=1e-4)
    # 用完整公式就不該有「不符」的警告
    assert "不符" not in caplog.text


def test_tpex_detail_extraction() -> None:
    """換股股數與退還股款都要取得出來；沒有退還股款時為 0"""

    detail: str = tpex_detail(shares=550.0, refund=0.0)

    assert CorporateActionCleaner.extract_tpex_share_exchange(detail) == 550.0
    assert CorporateActionCleaner.extract_tpex_cash_refund(detail) == 0.0
    assert CorporateActionCleaner.extract_tpex_share_exchange("<table></table>") is None


# === 缺資料不得靜默入庫 ===
def test_rows_without_price_are_dropped(cleaner: CorporateActionCleaner) -> None:
    """前收盤價為 `--`（站方表示無資料）時算不出倍率，該列不入庫"""

    raw: pd.DataFrame = pd.DataFrame(
        [["114/02/12", "2025", "千興", "--", "17.09", "", "", "", "", "彌補虧損", ""]],
        columns=TWSE_FIELDS,
    )

    assert cleaner.clean(raw, source="twse") is None


def test_missing_column_aborts_cleaning(cleaner: CorporateActionCleaner) -> None:
    """
    欄名對不上代表版面改制，拋例外而不是整批靜靜略過

    **不可硬取位置**：欄位錯位是靜默的錯，會一路錯到還原價。也不回 None：
    那與「這段期間沒有公司行動」長得一樣，更新會無聲地停止。
    """

    raw: pd.DataFrame = pd.DataFrame([["114/02/12", "2025"]], columns=["日期", "代號"])

    with pytest.raises(ColumnLayoutError):
        cleaner.clean(raw, source="twse")


# === Loader ===
@pytest.fixture
def loader(tmp_path, monkeypatch) -> Iterator[CorporateActionLoader]:
    """建一個寫進臨時 SQLite 的 loader（不碰 production DB）"""

    db_path = tmp_path / "test.db"
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.corporate_action_loader.TW_STOCK_DB_PATH", db_path
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.corporate_action_loader."
        "CORPORATE_ACTION_DOWNLOADS_PATH",
        tmp_path / "downloads",
    )
    instance = CorporateActionLoader()
    yield instance
    instance.disconnect()


def make_row(source: str, ratio: float = 2.0) -> Dict[str, object]:
    return {
        "date": "2025-06-18",
        "stock_id": "0050",
        "證券名稱": "元大台灣50",
        "停止買賣前收盤價": 100.0,
        "恢復買賣參考價": 100.0 * ratio,
        "調整倍率": ratio,
        "事件類型": "分割",
        "原因": "測試",
        "資料來源": source,
    }


def test_exchange_source_wins_over_detected(loader: CorporateActionLoader) -> None:
    """
    跨來源重複時交易所優先

    偵測值是從價格反推的近似，端點給的是官方參考價。
    **不可依檔名字典序決定**。
    """

    merged: pd.DataFrame = pd.DataFrame(
        [make_row("detected", ratio=2.0), make_row("twse", ratio=3.0)]
    )

    deduped: pd.DataFrame = dedup_by_source_priority(
        merged, loader.SOURCE_PRIORITY, label="corporate_action"
    )

    assert len(deduped) == 1
    assert deduped.iloc[0]["資料來源"] == "twse"
    assert deduped.iloc[0]["調整倍率"] == 3.0


def test_upsert_is_idempotent(loader: CorporateActionLoader) -> None:
    """
    重跑不得產生重複列，也不得整批拋錯

    端點是區間查詢，每次回補都會把同一年再取一次；`append` 會因主鍵衝突
    整批拋錯，讓「重跑」與「真的出錯」無法區分。
    """

    df: pd.DataFrame = pd.DataFrame([make_row("twse")])

    loader.upsert(df)
    loader.upsert(df)
    loader.conn.commit()

    count: int = loader.conn.execute(
        f"SELECT COUNT(*) FROM {CORPORATE_ACTION_TABLE_NAME}"
    ).fetchone()[0]
    assert count == 1


def test_upsert_replaces_on_conflict(loader: CorporateActionLoader) -> None:
    """同鍵不同值時以新值覆蓋（站方更正過的資料要進得來）"""

    loader.upsert(pd.DataFrame([make_row("twse", ratio=2.0)]))
    loader.upsert(pd.DataFrame([make_row("twse", ratio=4.0)]))
    loader.conn.commit()

    ratio: float = loader.conn.execute(
        f"SELECT 調整倍率 FROM {CORPORATE_ACTION_TABLE_NAME}"
    ).fetchone()[0]
    assert ratio == 4.0


def test_table_has_expected_schema(loader: CorporateActionLoader) -> None:
    """建表欄位與清洗輸出一致，主鍵為 (date, stock_id)"""

    info = loader.conn.execute(
        f"PRAGMA table_info('{CORPORATE_ACTION_TABLE_NAME}')"
    ).fetchall()
    columns: List[str] = [row[1] for row in info]
    primary_keys: List[str] = [row[1] for row in info if row[5]]

    assert columns == OUTPUT_COLUMNS
    assert primary_keys == ["date", "stock_id"]


# === 偵測器 ===
def test_detector_flags_leveraged_etf(
    tmp_path: Path, dao_factory: Callable[..., BaseDAO]
) -> None:
    """
    槓桿與反向 ETF 的漲跌幅上限是 ±20%，會被 15% 門檻誤報

    實測 2025-04-07 關稅重挫那天，00631L 等一整批同時跌到 −20%，那天它們
    **都沒有任何公司行動**。標記而不濾掉，是因為直接濾會連真正的反向分割
    一起藏起來（S1 查到 00632R 在 2024-12-11 做過反向分割）。
    """

    from core.pipeline.tw.cleaners.corporate_action_detector import (
        detect_unexplained_moves,
    )

    db_path: Path = tmp_path / "price.db"
    conn: sqlite3.Connection = sqlite3.connect(db_path)
    dao_factory(
        StockPriceDAO,
        conn=conn,
        records=[
            {"date": "2025-04-02", "stock_id": "00631L", "收盤價": 200.30},
            {"date": "2025-04-07", "stock_id": "00631L", "收盤價": 160.25},
            {"date": "2025-06-10", "stock_id": "0050", "收盤價": 188.65},
            {"date": "2025-06-18", "stock_id": "0050", "收盤價": 47.57},
        ],
    )

    result: pd.DataFrame = detect_unexplained_moves(conn=conn)
    conn.close()

    flags = dict(zip(result["stock_id"], result["疑似槓桿反向ETF"]))
    assert flags["00631L"] is True or flags["00631L"] == 1
    assert flags["0050"] is False or flags["0050"] == 0

    # 0050 的 8 日停牌是公司行動的線索；槓桿 ETF 跌停沒有停牌
    stop_days = dict(zip(result["stock_id"], result["停牌日數"]))
    assert stop_days["0050"] == 8
