import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pytest

from core.api.tw.futures_continuous_api import FuturesContinuousAPI
from core.backtest.report import futures_reporter as futures_reporter_module
from core.backtest.report.futures_reporter import FuturesBacktestReporter
from core.config import FUTURES_CONTINUOUS_TABLE_NAME
from core.dao.tw.futures_continuous_dao import FuturesContinuousDAO
from core.market.tw.futures_roll import FuturesRollConfig
from core.models import FuturesAccount
from core.strategies.futures import BaseFuturesStrategy
from core.utils import FuturesAdjustMethod, FuturesRollRule, FuturesSession, Scale

"""
期貨對標序列測試：連續合約優先、查不到才退回近月拼接

**兩種序列不是同一條曲線**：近月拼接在換月當天有一段展期價差造成的**假跳空**，
連續合約已經把它調整掉。混著看會讓「策略贏了對標多少」在換月月份整段失真，
而圖上完全看不出來用的是哪一種——故實際採用哪一種必須標在圖表註腳。

不連網路、不碰正式的 `tw_futures.db`。
"""

PRODUCT: str = "TX"
START: datetime.date = datetime.date(2024, 1, 2)
END: datetime.date = datetime.date(2024, 1, 5)


class ScriptedFuturesStrategy(BaseFuturesStrategy):
    """只提供 reporter 需要的欄位的最小策略"""

    def __init__(self, roll_rule: Optional[FuturesRollRule] = None) -> None:
        super().__init__()

        self.strategy_name: str = "BenchmarkSeries"
        self.init_capital: float = 1_000_000.0
        self.products: List[str] = [PRODUCT]
        self.scale: Scale = Scale.DAY
        self.session: FuturesSession = FuturesSession.DAY
        self.start_date: datetime.date = START
        self.end_date: datetime.date = END

        if roll_rule is not None:
            self.roll_config = FuturesRollConfig(rule=roll_rule)

    def setup_account(self, account: FuturesAccount) -> None:
        self.account: FuturesAccount = account

    def setup_apis(self, feed=None) -> None:
        pass

    def check_open_signal(self, quotes):
        return []

    def check_close_signal(self, quotes):
        return []

    def check_stop_loss_signal(self, quotes):
        return []

    def calculate_position_size(self, quotes, action):
        return []


# === API：讀得回寫進去的那一組，且不會拿到別組 ===
def make_continuous_db(tmp_path: Path) -> sqlite3.Connection:
    """建一個只有 TX 的 futures_continuous 暫存庫"""

    conn: sqlite3.Connection = sqlite3.connect(tmp_path / "tw_futures.db")
    dao: FuturesContinuousDAO = FuturesContinuousDAO(conn=conn)
    dao.ensure_table()

    rows: List[tuple] = []
    for offset, (method, close) in enumerate(
        [(FuturesAdjustMethod.BACKWARD.value, 18000.0), ("NONE", 17500.0)]
    ):
        for day, price in enumerate([close, close + 10, close + 20]):
            rows.append(
                (
                    (START + datetime.timedelta(days=day)).isoformat(),
                    PRODUCT,
                    FuturesSession.DAY.value,
                    method,
                    FuturesRollRule.LAST_TRADING_DAY.value,
                    "202401",
                    price,
                    price,
                    price,
                    price,
                    1000,
                    price,
                    100,
                    0,
                    0.0,
                    float(offset),
                )
            )

    conn.executemany(
        f"INSERT INTO {FUTURES_CONTINUOUS_TABLE_NAME} VALUES ({','.join('?' * 16)})",
        rows,
    )
    conn.commit()
    return conn


def test_api_returns_only_the_requested_adjust_method(tmp_path: Path) -> None:
    """
    同一天有三種調整方式，查詢必須逐一指定——**混著拿等於疊三條曲線**
    """

    conn: sqlite3.Connection = make_continuous_db(tmp_path)
    api: FuturesContinuousAPI = FuturesContinuousAPI(conn=conn)
    try:
        backward: pd.Series = api.get_close_series(
            PRODUCT, START, END, method=FuturesAdjustMethod.BACKWARD
        )
    finally:
        conn.close()

    assert len(backward) == 3
    assert backward.iloc[0] == 18000.0
    assert backward.index[0] == START


def test_api_returns_empty_for_a_series_that_was_never_built(tmp_path: Path) -> None:
    """
    沒建過的那一組回空表，**不自行換一組補上去**

    換一組等於讓呼叫端拿到它沒有要求的曲線，而且毫無徵兆。
    """

    conn: sqlite3.Connection = make_continuous_db(tmp_path)
    api: FuturesContinuousAPI = FuturesContinuousAPI(conn=conn)
    try:
        series: pd.Series = api.get_close_series(
            PRODUCT, START, END, roll_rule=FuturesRollRule.OPEN_INTEREST
        )
    finally:
        conn.close()

    assert series.empty


# === Reporter：連續合約優先，查不到退回近月拼接 ===
class StubContinuousAPI:
    """可控空／非空的假 API，並記下被問了哪一組設定"""

    calls: List[tuple] = []
    series: pd.Series = pd.Series(dtype=float)

    def __init__(self, conn=None) -> None:
        pass

    def get_close_series(
        self, product, start_date, end_date, session=None, method=None, roll_rule=None
    ) -> pd.Series:
        StubContinuousAPI.calls.append((product, session, roll_rule))
        return StubContinuousAPI.series

    def close(self) -> None:
        pass


class StubFuturesPriceAPI:
    """近月拼接用的 `FuturesPriceAPI` 替身；只需要能被關閉"""

    def close(self) -> None:
        """不持有連線，無事可做"""


@pytest.fixture
def stub_continuous(monkeypatch: pytest.MonkeyPatch):
    """把 reporter 用的連續合約 API 換成 stub"""

    StubContinuousAPI.calls = []
    StubContinuousAPI.series = pd.Series(dtype=float)
    monkeypatch.setattr(
        futures_reporter_module, "FuturesContinuousAPI", StubContinuousAPI
    )
    return StubContinuousAPI


def test_reporter_prefers_the_continuous_series(stub_continuous, tmp_path) -> None:
    """連續合約查得到就用它，不再走近月拼接"""

    stub_continuous.series = pd.Series(
        [18000.0, 18010.0], index=[START, START + datetime.timedelta(days=1)]
    )

    reporter: FuturesBacktestReporter = FuturesBacktestReporter(
        ScriptedFuturesStrategy(), tmp_path
    )

    assert list(reporter.benchmark_price) == [18000.0, 18010.0]
    assert reporter.benchmark_series_kind == reporter.CONTINUOUS_SERIES_LABEL
    assert "連續合約" in reporter.get_benchmark_note()


def test_reporter_asks_for_the_strategy_roll_rule(stub_continuous, tmp_path) -> None:
    """
    對標的換月規則要跟著策略走

    對標在最後交易日換、策略提前 N 日換的話，換月那幾天比的是不同的東西。
    """

    stub_continuous.series = pd.Series([18000.0], index=[START])

    FuturesBacktestReporter(
        ScriptedFuturesStrategy(roll_rule=FuturesRollRule.OPEN_INTEREST), tmp_path
    )

    assert stub_continuous.calls == [
        (PRODUCT, FuturesSession.DAY, FuturesRollRule.OPEN_INTEREST)
    ]


def test_reporter_falls_back_to_the_near_month_splice(
    stub_continuous, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    連續合約沒有那一組時退回近月拼接，並在註腳標明

    **不可靜默退回**：兩種口徑的接點行為不同，圖上不標的話，看圖的人
    無從判斷換月那幾天的落差是策略造成的還是展期價差造成的。
    """

    spliced: pd.Series = pd.Series([17000.0], index=[START])
    monkeypatch.setattr(
        FuturesBacktestReporter,
        "build_near_month_close_series",
        lambda self, futures_price: spliced,
    )
    # 拼接本身已換成固定序列，reporter 自己 new 的 `FuturesPriceAPI` 只剩開關連線；
    # 不換的話它會去開 `tw_futures.db`，沒有資料庫的環境（CI）當場失敗
    monkeypatch.setattr(futures_reporter_module, "FuturesPriceAPI", StubFuturesPriceAPI)

    reporter: FuturesBacktestReporter = FuturesBacktestReporter(
        ScriptedFuturesStrategy(), tmp_path
    )

    assert list(reporter.benchmark_price) == [17000.0]
    assert reporter.benchmark_series_kind == reporter.NEAR_MONTH_SERIES_LABEL
    assert "假跳空" in reporter.get_benchmark_note()
