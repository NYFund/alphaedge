import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pandas as pd
import pytest

from core.config import FUTURES_CONTINUOUS_TABLE_NAME, TW_FUTURES_DB_PATH
from core.config.schema import FuturesPriceColumn
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.market.tw.futures_calendar import FuturesCalendar
from core.market.tw.futures_roll import FuturesRollPlanner
from core.pipeline.tw.loaders.futures_continuous_loader import (
    FuturesContinuousLoader,
)
from core.pipeline.tw.updaters.futures_continuous_updater import (
    FuturesContinuousUpdater,
)
from core.utils import FuturesAdjustMethod, FuturesRollRule

"""
連續合約與換月規則測試

**連續合約唯一的難點是「調整方向」，而方向錯了不會報錯**：把加號寫成減號同樣
產出一條連續的序列、同樣通過還原檢查，只是每個換月接點的日變動都變成
「真實變動 ＋ 兩倍展期價差」。唯一抓得到的檢查是本檔的
`test_no_artificial_gap_at_roll()`——實作時方向確實寫反過，就是被它抓出來的。

其餘三件被釘住的事：
1. **換月只往前不回頭**：規則若想換回更近的月份一律沿用昨天那個，
   真實轉倉不可能「換回去」。
2. **展期價差與比例必須取自同一天的兩個契約**，否則兩種調整方式會在同一個
   接點對不上。
3. **未沖銷契約量缺漏不可當成 0**：那會讓近月被判定為輸給次月而誤觸換月。
"""


CLOSE: str = FuturesPriceColumn.CLOSE.value
TRADING_DAYS: List[str] = [
    "2024-03-18",
    "2024-03-19",
    "2024-03-20",  # 202403 的最後交易日（第三個星期三）
    "2024-03-21",
    "2024-03-22",
]


def make_calendar() -> FuturesCalendar:
    """涵蓋 2024-03 換月週的日曆"""

    return FuturesCalendar([datetime.date.fromisoformat(d) for d in TRADING_DAYS])


def make_price_df(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """組出 `futures_price_daily` 形狀的行情表"""

    return pd.DataFrame(
        [
            {
                "date": row["date"],
                "product": "TX",
                "expiry": row["expiry"],
                "session": "day",
                FuturesPriceColumn.OPEN.value: row["close"],
                FuturesPriceColumn.HIGH.value: row["close"],
                FuturesPriceColumn.LOW.value: row["close"],
                CLOSE: row["close"],
                FuturesPriceColumn.VOLUME.value: 1000,
                FuturesPriceColumn.SETTLEMENT.value: row["close"],
                FuturesPriceColumn.OPEN_INTEREST.value: row.get("oi", 100),
            }
            for row in rows
        ]
    )


# 近月 202403 與次月 202404 並行；202403 於 03-20 到期
PRICES: List[Dict[str, Any]] = [
    {"date": "2024-03-18", "expiry": "202403", "close": 20000, "oi": 900},
    {"date": "2024-03-18", "expiry": "202404", "close": 19900, "oi": 100},
    {"date": "2024-03-19", "expiry": "202403", "close": 20100, "oi": 800},
    {"date": "2024-03-19", "expiry": "202404", "close": 20010, "oi": 300},
    {"date": "2024-03-20", "expiry": "202403", "close": 20200, "oi": 400},
    {"date": "2024-03-20", "expiry": "202404", "close": 20120, "oi": 900},
    {"date": "2024-03-21", "expiry": "202404", "close": 20300, "oi": 950},
    {"date": "2024-03-22", "expiry": "202404", "close": 20250, "oi": 960},
]


@pytest.fixture
def updater() -> FuturesContinuousUpdater:
    """
    只取用建表邏輯的 updater

    `FuturesContinuousUpdater.__new__` 跳過 `setup()`，避免連資料庫——
    本檔驗的是演算法，不是入庫。
    """

    return FuturesContinuousUpdater.__new__(FuturesContinuousUpdater)


def build_series(
    updater: FuturesContinuousUpdater, rule=FuturesRollRule.LAST_TRADING_DAY
):
    """依指定換月規則建出未調整的序列"""

    price_df: pd.DataFrame = make_price_df(PRICES)
    planner: FuturesRollPlanner = FuturesRollPlanner(make_calendar(), rule=rule)
    schedule = planner.build_roll_schedule(
        dates=[datetime.date.fromisoformat(d) for d in TRADING_DAYS],
        expiries_by_date=updater.build_expiries_by_date(price_df),
        open_interest_by_date=updater.build_open_interest_by_date(price_df),
    )
    return updater.build_series(price_df, schedule)


# === 換月規則 ===
def test_last_trading_day_rule_keeps_near_month_until_expiry(updater) -> None:
    """`LAST_TRADING_DAY`：最後交易日**當天仍是近月**，隔天才換"""

    series: pd.DataFrame = build_series(updater)
    by_date: Dict[str, str] = dict(zip(series["date"], series["expiry"]))

    assert by_date["2024-03-20"] == "202403"  # 結算日當天不換
    assert by_date["2024-03-21"] == "202404"
    assert list(series["roll_flag"]) == [0, 0, 0, 1, 0]


def test_days_before_expiry_rule_rolls_earlier(updater) -> None:
    """`DAYS_BEFORE_EXPIRY`：提前 N 個**交易日**換月，用曆日算會在連假位移"""

    series: pd.DataFrame = build_series(
        updater, rule=FuturesRollRule.DAYS_BEFORE_EXPIRY
    )
    by_date: Dict[str, str] = dict(zip(series["date"], series["expiry"]))

    assert by_date["2024-03-19"] == "202404"  # 到期前 1 個交易日就換
    assert by_date["2024-03-18"] == "202403"


def test_open_interest_rule_rolls_on_crossover(updater) -> None:
    """`OPEN_INTEREST`：次月未沖銷量超過近月的那天換"""

    series: pd.DataFrame = build_series(updater, rule=FuturesRollRule.OPEN_INTEREST)
    by_date: Dict[str, str] = dict(zip(series["date"], series["expiry"]))

    assert by_date["2024-03-19"] == "202403"  # 800 > 300，還沒交叉
    assert by_date["2024-03-20"] == "202404"  # 900 > 400，交叉


def test_open_interest_missing_does_not_force_a_roll() -> None:
    """
    未沖銷量缺漏時**退回近月**

    夜盤的未沖銷量本來就是 NULL；當成 0 會讓近月被判定為輸給次月而誤觸換月。
    """

    planner: FuturesRollPlanner = FuturesRollPlanner(
        make_calendar(), rule=FuturesRollRule.OPEN_INTEREST
    )

    assert (
        planner.resolve_active_expiry(
            datetime.date(2024, 3, 18),
            ["202403", "202404"],
            {"202403": None, "202404": 999},
        )
        == "202403"
    )


def test_roll_never_goes_backwards() -> None:
    """
    **換月只往前不回頭**

    未沖銷量交叉後又反轉時，若跟著換回近月，序列會憑空生出一段價差——
    真實轉倉是實際的買賣行為，不可能「換回去」。
    """

    planner: FuturesRollPlanner = FuturesRollPlanner(
        make_calendar(), rule=FuturesRollRule.OPEN_INTEREST
    )
    dates: List[datetime.date] = [
        datetime.date(2024, 3, 18),
        datetime.date(2024, 3, 19),
    ]
    schedule = planner.build_roll_schedule(
        dates=dates,
        expiries_by_date={date: ["202403", "202404"] for date in dates},
        open_interest_by_date={
            dates[0]: {"202403": 100, "202404": 900},  # 交叉 → 換到次月
            dates[1]: {"202403": 900, "202404": 100},  # 反轉 → 不可換回
        },
    )

    assert schedule[dates[0]] == "202404"
    assert schedule[dates[1]] == "202404"


def test_weekly_contracts_are_excluded_by_default() -> None:
    """週契約與月契約是不同商品，混進來會讓連續合約每週換一次月"""

    planner: FuturesRollPlanner = FuturesRollPlanner(make_calendar())

    assert planner.filter_expiries(["202403", "202403W2", "202404"]) == [
        "202403",
        "202404",
    ]


# === 展期價差與調整 ===
def test_roll_gap_comes_from_the_same_day(updater) -> None:
    """
    展期價差取「同一天、兩個契約」的收盤價差

    換月當日舊契約已無報價，故往前找最近一個兩者都有報價的交易日：
    03-20 的 202404 收 20,120、202403 收 20,200，價差 −80。
    """

    series: pd.DataFrame = build_series(updater)
    roll_row: pd.Series = series[series["roll_flag"] == 1].iloc[0]

    assert roll_row["date"] == "2024-03-21"
    assert roll_row["roll_gap"] == pytest.approx(-80.0)
    assert roll_row["roll_ratio"] == pytest.approx(20120 / 20200)


def test_roll_gap_skips_a_day_with_no_trade(updater) -> None:
    """
    換月前一天新契約整天零成交（收盤 NaN）時，往前找再前一天的價差

    列存在不代表有價：cleaner 把來源的 `-` 保留成 NaN。以前只檢查列在不在，
    NaN 相減得 NaN，由後往前累加時一次污染，換月之前的整段調整價全變 NaN、不報錯。
    """

    prices: List[Dict[str, Any]] = [
        dict(row, close=float("nan"))
        if (row["date"], row["expiry"]) == ("2024-03-20", "202404")
        else row
        for row in PRICES
    ]
    price_df: pd.DataFrame = make_price_df(prices)
    planner: FuturesRollPlanner = FuturesRollPlanner(
        make_calendar(), rule=FuturesRollRule.LAST_TRADING_DAY
    )
    schedule = planner.build_roll_schedule(
        dates=[datetime.date.fromisoformat(d) for d in TRADING_DAYS],
        expiries_by_date=updater.build_expiries_by_date(price_df),
        open_interest_by_date=updater.build_open_interest_by_date(price_df),
    )
    series: pd.DataFrame = updater.build_series(price_df, schedule)
    roll_row: pd.Series = series[series["roll_flag"] == 1].iloc[0]

    # 03-19：202404 收 20,010、202403 收 20,100
    assert roll_row["roll_gap"] == pytest.approx(-90.0)
    for method in (FuturesAdjustMethod.BACKWARD, FuturesAdjustMethod.RATIO):
        adjusted: pd.DataFrame = updater.apply_adjustment(series.copy(), method)
        assert adjusted["adj_factor"].notna().all()


def test_no_artificial_gap_at_roll(updater) -> None:
    """
    **調整後的換月日變動 ＝ 新契約自己的日變動**

    這是唯一抓得到「調整方向寫反」的檢查：方向反了同樣連續、同樣可還原，
    只是接點的變動會變成「真實變動 ＋ 兩倍價差」。
    """

    series: pd.DataFrame = build_series(updater)
    adjusted: pd.DataFrame = updater.apply_adjustment(
        series.copy(), FuturesAdjustMethod.BACKWARD
    )

    index: int = list(adjusted["date"]).index("2024-03-21")
    change: float = adjusted.iloc[index][CLOSE] - adjusted.iloc[index - 1][CLOSE]

    # 202404 自己的變動：20,300 − 20,120 ＝ 180
    assert change == pytest.approx(180.0)


def test_backward_adjustment_keeps_the_latest_segment_intact(updater) -> None:
    """逆向調整以**最新一段為基準**：最後一列的調整量為 0，價格等於真實成交價"""

    series: pd.DataFrame = build_series(updater)
    adjusted: pd.DataFrame = updater.apply_adjustment(
        series.copy(), FuturesAdjustMethod.BACKWARD
    )

    assert adjusted.iloc[-1]["adj_factor"] == 0.0
    assert adjusted.iloc[-1][CLOSE] == 20250
    # 換月之前的每一列都被調整（價差 −80）
    assert adjusted.iloc[0]["adj_factor"] == pytest.approx(-80.0)
    assert adjusted.iloc[0][CLOSE] == pytest.approx(20000 - 80)


def test_adjustments_are_reversible(updater) -> None:
    """
    **展期價差可被還原檢查**（本步驟的驗收條件）

    BACKWARD：原始價 ＝ 調整價 − adj_factor；RATIO：原始價 ＝ 調整價 ÷ adj_factor。
    """

    raw: pd.DataFrame = build_series(updater)

    backward: pd.DataFrame = updater.apply_adjustment(
        raw.copy(), FuturesAdjustMethod.BACKWARD
    )
    ratio: pd.DataFrame = updater.apply_adjustment(
        raw.copy(), FuturesAdjustMethod.RATIO
    )

    for index in range(len(raw)):
        original: float = raw.iloc[index][CLOSE]
        assert backward.iloc[index][CLOSE] - backward.iloc[index][
            "adj_factor"
        ] == pytest.approx(original)
        assert ratio.iloc[index][CLOSE] / ratio.iloc[index][
            "adj_factor"
        ] == pytest.approx(original)


def test_none_method_keeps_raw_prices(updater) -> None:
    """`NONE` 是對照組：完全不調整，接點的假跳空原樣保留"""

    raw: pd.DataFrame = build_series(updater)
    none_series: pd.DataFrame = updater.apply_adjustment(
        raw.copy(), FuturesAdjustMethod.NONE
    )

    assert list(none_series[CLOSE]) == [20000, 20100, 20200, 20300, 20250]
    assert set(none_series["adj_factor"]) == {0.0}


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能驗連續合約"
)
def test_real_continuous_table_is_consistent() -> None:
    """
    以真實表驗證：換月次數、還原一致性與接點無假跳空

    TX 2015-01 ~ 2026-08 共 2,842 個交易日、140 次換月（每個已到期月契約一次）。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        continuous: pd.DataFrame = pd.read_sql_query(
            f"SELECT * FROM {FUTURES_CONTINUOUS_TABLE_NAME} "
            f"WHERE product = 'TX' AND method = 'BACKWARD' "
            f"AND roll_rule = 'LAST_TRADING_DAY' ORDER BY date",
            conn,
        )
        raw: pd.DataFrame = pd.read_sql_query(
            "SELECT date, expiry, 收盤價 FROM futures_price_daily "
            "WHERE product = 'TX' AND session = 'day'",
            conn,
        )
    finally:
        conn.close()

    if continuous.empty:
        pytest.skip("連續合約表尚未建立（`--target futures_continuous`）")

    merged: pd.DataFrame = continuous.merge(
        raw, on=["date", "expiry"], suffixes=("", "_raw")
    )

    assert len(merged) == len(continuous), "每一列都應該對得回原始行情"
    restored: pd.Series = merged[CLOSE] - merged["adj_factor"]
    assert (restored - merged[f"{CLOSE}_raw"]).abs().max() < 1e-6

    # 最新一段未被調整
    assert continuous.iloc[-1]["adj_factor"] == pytest.approx(0.0)

    # 每個換月接點的日變動都等於新契約自己的變動（無假跳空）
    raw_by_key: Dict[tuple, float] = {
        (row.date, row.expiry): getattr(row, CLOSE) for row in raw.itertuples()
    }
    dates: List[str] = list(continuous["date"])
    mismatched: List[str] = []

    for _index, row in continuous[continuous["roll_flag"] == 1].iterrows():
        position: int = dates.index(row["date"])
        adjusted_change: float = (
            continuous.iloc[position][CLOSE] - continuous.iloc[position - 1][CLOSE]
        )
        new_today = raw_by_key.get((row["date"], row["expiry"]))
        new_yesterday = raw_by_key.get(
            (continuous.iloc[position - 1]["date"], row["expiry"])
        )
        if new_today is None or new_yesterday is None:
            continue
        if abs(adjusted_change - (new_today - new_yesterday)) > 1e-6:
            mismatched.append(row["date"])

    assert not mismatched, f"換月接點出現假跳空：{mismatched[:5]}"


# === 整段重建 ===
def make_db_updater(tmp_path: Path) -> FuturesContinuousUpdater:
    """組一支只碰暫存 DB、行情由記憶體提供的 updater"""

    dao: FuturesContinuousDAO = FuturesContinuousDAO(db_path=tmp_path / "test.db")
    updater: FuturesContinuousUpdater = FuturesContinuousUpdater.__new__(
        FuturesContinuousUpdater
    )
    updater.dao = dao
    updater.loader = FuturesContinuousLoader(dao=dao)
    updater.loader.continuous_dir = tmp_path
    updater.price_api = SimpleNamespace(
        get_range=lambda start_date, end_date, product=None, session=None: (
            make_price_df(
                [
                    row
                    for row in PRICES
                    if start_date
                    <= datetime.date.fromisoformat(row["date"])
                    <= end_date
                ]
            )
        ),
        get_trading_days=lambda start_date, end_date, product=None: [
            date
            for date in (datetime.date.fromisoformat(d) for d in TRADING_DAYS)
            if start_date <= date <= end_date
        ],
    )
    return updater


def test_rebuild_removes_rows_before_the_new_start(tmp_path: Path) -> None:
    """
    以較晚的起日重建時，起日之前的舊列必須被清掉

    `INSERT OR REPLACE` 只覆寫主鍵相同的列。舊列留著的話，它們帶的是上一代的
    `adj_factor`——兩代交界那次換月的價差沒有被調整，而序列表面上仍然連續。
    """

    updater: FuturesContinuousUpdater = make_db_updater(tmp_path)
    try:
        updater.update(
            products=["TX"],
            start_date=datetime.date(2024, 3, 18),
            end_date=datetime.date(2024, 3, 22),
        )
        first_dates: List[str] = [
            row[0]
            for row in updater.dao.conn.execute(
                f"SELECT DISTINCT date FROM {FUTURES_CONTINUOUS_TABLE_NAME} "
                f"ORDER BY date"
            )
        ]

        updater.update(
            products=["TX"],
            start_date=datetime.date(2024, 3, 21),
            end_date=datetime.date(2024, 3, 22),
        )
        rebuilt_dates: List[str] = [
            row[0]
            for row in updater.dao.conn.execute(
                f"SELECT DISTINCT date FROM {FUTURES_CONTINUOUS_TABLE_NAME} "
                f"ORDER BY date"
            )
        ]
    finally:
        updater.close()

    assert first_dates[0] == "2024-03-18"
    assert rebuilt_dates == ["2024-03-21", "2024-03-22"]
