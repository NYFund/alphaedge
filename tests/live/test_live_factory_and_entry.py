import datetime
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import (
    TW_FUTURES_SEGMENTS,
    TW_STOCK_SEGMENTS,
    UnsupportedMarketError,
    _merge_schedules,
    build_live_trader,
)
from core.live.segment import SegmentSchedule, SegmentWindow
from core.live.trader import LiveTrader, StrategyContext
from core.strategies.base import BaseStrategy
from core.utils import ExecutionStyle, ExecutionTiming, InstrumentType, LiveHook, Market

from .conftest import FakeBroker

"""
實盤 factory 與 CLI 入口

CLI 這一層擋的是**一個參數打錯就連到正式環境**。防呆一律在建立任何連線之前：
`--production` 打錯的代價是真的下單，那不是一個可以「先連連看再說」的操作。
"""

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


class LiveStockStrategy(BaseStrategy):
    """最小的台股實盤策略"""

    def __init__(self) -> None:
        super().__init__()
        self.market = Market.TW
        self.instrument_type = InstrumentType.STOCK
        self.init_capital = 1_000_000.0
        self.live_ready = True
        self.live_schedule = {
            LiveHook.OPEN.value: ExecutionTiming.AT_CLOSE,
            LiveHook.CLOSE.value: ExecutionTiming.AT_CLOSE,
        }
        self.live_execution = ExecutionStyle.MARKET
        self.symbols: List[str] = ["2330"]

    def setup_account(self, account: Any) -> None:
        self.account = account


class AnotherLiveStockStrategy(LiveStockStrategy):
    """第二支台股策略；只用來驗多策略共用單例"""


class LiveFuturesStrategy(LiveStockStrategy):
    """最小的台期貨實盤策略"""

    def __init__(self) -> None:
        super().__init__()
        self.instrument_type = InstrumentType.FUTURE


class UnsupportedStrategy(LiveStockStrategy):
    """沒有對應實作的組合"""

    def __init__(self) -> None:
        super().__init__()
        self.market = Market.US


@pytest.fixture(autouse=True)
def in_memory_history_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    實盤資料源的歷史資料庫一律換成空的 in-memory 連線

    factory 建資料源時用的是預設的 `tw_stock.db`／`tw_futures.db` 路徑，
    本檔驗的是組裝邏輯，不需要任何歷史資料；不換的話，沒有資料庫的環境（CI）
    會在 `connect_sqlite` 當場失敗，而有資料庫的本機照樣綠，紅綠只取決於機器。
    """

    def connect_in_memory(db_path: Any, read_only: bool = False) -> sqlite3.Connection:
        return sqlite3.connect(":memory:")

    monkeypatch.setattr(stock_live_datafeed, "connect_sqlite", connect_in_memory)
    monkeypatch.setattr(futures_live_datafeed, "connect_sqlite", connect_in_memory)


def build(
    strategies: List[BaseStrategy], dao: LiveTradeDAO, **kwargs: Any
) -> LiveTrader:
    return build_live_trader(
        strategies,
        broker=FakeBroker(),
        dao=dao,
        run_id="20260920133000",
        now_provider=lambda: datetime.datetime(2026, 9, 20, 13, 30),
        **kwargs,
    )


# === factory ===
def test_single_strategy_is_not_a_special_path(dao: LiveTradeDAO) -> None:
    """
    清單只有一支時行為與多策略完全相同

    **多策略不是另一條程式路徑**：分成兩條的話，單策略那條會慢慢長出
    只有它才有的行為，而那些行為在多策略下是錯的。
    """

    trader: LiveTrader = build([LiveStockStrategy()], dao)

    assert len(trader.contexts) == 1
    assert trader.allocator.quotas == {"LiveStockStrategy": 1_000_000.0}


def test_parity_checks_only_the_strategies_of_this_process(dao: LiveTradeDAO) -> None:
    """
    盤後 parity 的比對範圍＝本次載入的策略

    股票線與期貨線各自一個行程、共用同一個紀錄庫；不傳範圍的話，
    一個行程會替另一個行程的策略記假的未解釋差異。
    """

    trader: LiveTrader = build([LiveStockStrategy()], dao)

    assert trader.after_close.parity_checker.strategy_names == {"LiveStockStrategy"}


def test_order_manager_only_takes_over_this_process_strategies(
    dao: LiveTradeDAO,
) -> None:
    """委託接管的範圍＝本次載入的策略；與 parity 同一個理由（兩個行程共用紀錄庫）"""

    trader: LiveTrader = build([LiveStockStrategy()], dao)

    assert trader.order_manager.strategy_names == {"LiveStockStrategy"}


def test_singletons_are_shared_across_strategies(dao: LiveTradeDAO) -> None:
    """
    券商與委託管理跨策略共用一份，資料源與帳戶則每支策略各一份

    限流與委託回報都是**帳戶級**的：拆成多份時每個都以為自己還有額度，
    合起來必然超額，而且誰都不知道是誰用掉的。資金分配同樣只有一個，
    額度在它底下按策略名各記一筆。
    """

    trader: LiveTrader = build([LiveStockStrategy(), AnotherLiveStockStrategy()], dao)

    assert len({id(context.data_feed) for context in trader.contexts}) == 2
    assert len({id(context.account) for context in trader.contexts}) == 2
    assert trader.order_manager.broker is trader.broker
    assert len(trader.allocator.quotas) == 2


def test_duplicate_strategy_names_are_rejected(dao: LiveTradeDAO) -> None:
    """
    策略名稱重複要在組裝時就擋下

    歸屬鏈以策略名為鍵，重名會讓兩支策略的部位與損益混在一起，
    而合計仍然正確——對帳看不出來。
    """

    with pytest.raises(ValueError, match="名稱重複"):
        build([LiveStockStrategy(), LiveStockStrategy()], dao)


def test_empty_strategy_list_is_rejected(dao: LiveTradeDAO) -> None:
    """空清單沒有東西可以跑；靜默成功會讓排程以為今天跑過了"""

    with pytest.raises(ValueError, match="策略清單為空"):
        build([], dao)


def test_unsupported_combination_names_both_axes(dao: LiveTradeDAO) -> None:
    """
    不支援的組合要把**兩個軸的值都印出來**

    只說「不支援」的話，看的人不知道是市場還是商品沒做。
    """

    with pytest.raises(UnsupportedMarketError) as error:
        build([UnsupportedStrategy()], dao)

    message: str = str(error.value)
    assert "market=" in message and "instrument_type=" in message


def test_run_record_carries_audit_fields(dao: LiveTradeDAO) -> None:
    """
    啟動紀錄要帶稽核欄位

    實盤出事時第一個要回答的是「那天那張單是哪一版程式送出的」。
    """

    build([LiveStockStrategy()], dao)

    row: Any = dao.conn.execute(
        "SELECT git_commit, shioaji_version, simulation FROM live_run"
    ).fetchone()

    assert row[0]  # 取不到時是 'unknown'，但不能是空的
    assert row[1]
    assert row[2] == 1


def test_run_record_carries_the_phase(dao: LiveTradeDAO) -> None:
    """
    啟動紀錄要寫段落名

    存活監控依段落名比對「該跑的有沒有跑」；寫空字串的話，它對每個段落都會報
    「沒有任何紀錄」——每天都誤報的監控很快就會被靜音。
    """

    build([LiveStockStrategy()], dao, phase="close")

    assert dao.conn.execute("SELECT phase FROM live_run").fetchone()[0] == "close"


def test_notional_uses_the_instrument_unit(dao: LiveTradeDAO) -> None:
    """
    金額換算要走商品規格

    台股一張是 1000 股；用「價 × 張」算金額會讓所有風控門檻都鬆了 1000 倍。
    """

    from core.models import StockOrder

    trader: LiveTrader = build([LiveStockStrategy()], dao)
    order: StockOrder = StockOrder(stock_id="2330", volume=2, price=1000.0)

    assert trader.contexts[0].notional(order) == pytest.approx(2_000_000.0)


def test_each_market_gets_its_own_execution_model(dao: LiveTradeDAO) -> None:
    """
    執行層依市場注入，漲跌停走資料源取合約的同一條路徑

    沒注入的策略每一張委託都會在送出前被擋下；注入錯市場的話，
    期貨會送出股票的保護價限價、股票會送出它沒有的範圍市價。
    """

    from core.live.execution.futures import FuturesExecutionModel
    from core.live.execution.stock import StockExecutionModel

    stock: StrategyContext = build([LiveStockStrategy()], dao).contexts[0]
    futures: StrategyContext = build([LiveFuturesStrategy()], dao).contexts[0]

    assert isinstance(stock.execution_model, StockExecutionModel)
    assert isinstance(futures.execution_model, FuturesExecutionModel)
    assert stock.execution_model.get_price_limits("2330") == (None, None)


def test_parity_backtest_uses_the_live_capital_and_holdings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    parity 的當日回測照實盤口徑：`live_capital` 與 `live_max_holdings`

    用研究回測的 `init_capital`／`max_holdings` 的話，宣告了實盤額度的策略每天都會
    因為張數與檔數不同而報未解釋差異。2026-10-06 試跑：實盤 40 萬切 3 檔、
    回測 100 萬切 10 檔，三筆未解釋差異全是設定不同，不是訊號漂移。
    """

    import core.live.factory as live_factory

    captured: Dict[str, Any] = {}

    class StubBacktester:
        submitted_orders: List[Any] = []

        def run(self) -> None:
            """不跑真的回測"""

    def fake_build(strategy: Any, write_artifacts: bool, overrides: Any) -> Any:
        captured["max_holdings"] = strategy.max_holdings
        captured["capital"] = overrides.capital
        return StubBacktester()

    monkeypatch.setattr(live_factory, "build_backtester", fake_build)

    class Sized(LiveStockStrategy):
        def __init__(self) -> None:
            super().__init__()
            self.init_capital = 1_000_000.0
            self.max_holdings = 10
            self.live_capital = 400_000.0
            self.live_max_holdings = 3

    runner = live_factory.make_daily_backtest_runner([Sized()])
    runner("Sized", datetime.date(2026, 10, 6))

    assert captured == {"max_holdings": 3, "capital": 400_000.0}


# === 段落時窗 ===
def test_stock_close_window_starts_after_1325() -> None:
    """
    尾盤段 13:25 才開始送單

    13:25 之前送出的限價單會在逐筆交易時段就成交，成交價不是收盤價——
    與回測「以收盤價成交」的假設對不上，而且看起來完全正常。
    """

    window: SegmentWindow = TW_STOCK_SEGMENTS[ExecutionTiming.AT_CLOSE]

    assert window.submit_start == datetime.time(13, 25)
    assert window.submit_end == datetime.time(13, 29)


def test_drain_end_is_after_the_closing_auction() -> None:
    """
    收線時點要晚於收盤集合競價

    送完單就收線會讓當日成交明細出現一段空窗，而對帳會在錯的時點判定不一致。
    """

    window: SegmentWindow = TW_STOCK_SEGMENTS[ExecutionTiming.AT_CLOSE]

    assert window.drain_end > datetime.time(13, 30)


def test_overlapping_windows_are_intersected() -> None:
    """
    有交集時取交集（最晚開始、最早結束）

    取聯集的話，會在某個市場還沒開盤時送單，那些單會被退。
    """

    other: SegmentSchedule = {
        ExecutionTiming.AT_CLOSE: SegmentWindow(
            submit_start=datetime.time(13, 26),
            submit_end=datetime.time(13, 40),
            drain_end=datetime.time(13, 45),
        )
    }
    merged: SegmentSchedule = _merge_schedules([TW_STOCK_SEGMENTS, other])
    window: SegmentWindow = merged[ExecutionTiming.AT_CLOSE]

    assert window.submit_start == datetime.time(13, 26)  # 較晚的開始
    assert window.submit_end == datetime.time(13, 29)  # 較早的結束
    assert window.drain_end == datetime.time(13, 45)  # 最晚的收線


def test_disjoint_windows_are_refused_not_patched() -> None:
    """
    **時窗沒有交集時拒絕合併，不湊一個出來**

    台股尾盤是 13:25~13:29、期貨是 13:30~13:44，兩者根本沒有交集。
    硬湊會得到「開始晚於結束」的窗，那等於整段都不送單，而且不會有任何錯誤訊息。
    真正的解法是分開排程。
    """

    with pytest.raises(ValueError, match="沒有交集"):
        _merge_schedules([TW_STOCK_SEGMENTS, TW_FUTURES_SEGMENTS])


# 入口（參數防呆、段落流程、退出碼）的測試在 `tests/test_live_entry.py`


def test_mixing_markets_with_disjoint_windows_is_refused(dao: LiveTradeDAO) -> None:
    """
    台股與期貨的尾盤時窗不重疊，**同一次執行不可混跑**

    理由同 `test_disjoint_windows_are_refused_not_patched`；這條守的是連組裝層
    都要當場拒絕，而不是等到那個湊出來的時窗整段都不送單才發現。正解是分開排程。
    """

    with pytest.raises(ValueError, match="沒有交集"):
        build([LiveStockStrategy(), LiveFuturesStrategy()], dao)


# === 回測不相依券商 SDK ===
NO_SDK_PROBE: str = """
import builtins
import sys

_real = builtins.__import__


def _blocked(name, *args, **kwargs):
    if name == "shioaji" or name.startswith("shioaji."):
        raise ImportError("No module named 'shioaji'")
    return _real(name, *args, **kwargs)


builtins.__import__ = _blocked
import core.live.factory  # noqa: F401,E402

print("OK")
"""


def test_factory_imports_without_the_broker_sdk() -> None:
    """
    **沒裝券商 SDK 也要 import 得了 `core.live.factory`**

    這條邊界是「回測不相依券商 SDK」。它只要沒有東西釘住就會漂回去——
    而漂回去的方式不只一種：模組層級直接 `import shioaji` 會，
    模組層級 import 一個**自己**相依 SDK 的類別也會，後者從本檔看不出來。

    在子行程裡驗，才不會被本次測試階段已經載入的模組蓋掉。
    """

    result: subprocess.CompletedProcess = subprocess.run(
        [sys.executable, "-c", NO_SDK_PROBE],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    assert result.returncode == 0, (
        f"沒裝 shioaji 就 import 不了 core.live.factory：\n{result.stderr[-2000:]}"
    )
    assert "OK" in result.stdout
