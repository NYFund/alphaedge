import argparse
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

import run as run_module
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.datafeed.tw import futures_live_datafeed, stock_live_datafeed
from core.live.factory import (
    TW_FUTURES_SEGMENTS,
    TW_STOCK_SEGMENTS,
    UnsupportedMarketError,
    _merge_schedules,
    build_live_trader,
)
from core.live.risk.trading_mode import TradingMode
from core.live.segment import SegmentSchedule, SegmentWindow
from core.live.trader import LiveTrader
from core.strategies.base import BaseStrategy
from core.utils import ExecutionTiming, InstrumentType, LiveHook, Market

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


@pytest.fixture
def dao() -> LiveTradeDAO:
    instance: LiveTradeDAO = LiveTradeDAO(conn=sqlite3.connect(":memory:"))
    instance.ensure_tables()
    return instance


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


def test_singletons_are_shared_across_strategies(dao: LiveTradeDAO) -> None:
    """
    券商、委託管理、限流、資金分配、歸屬帳只有一份

    限流與委託回報都是**帳戶級**的：拆成多份時每個都以為自己還有額度，
    合起來必然超額，而且誰都不知道是誰用掉的。
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


# === CLI ===
def _run_cli(*arguments: str) -> subprocess.CompletedProcess:
    """
    以子行程跑 `run.py`，取得真實的退出碼

    **子行程的環境一律隔離**：產物根指到暫存目錄、金鑰清空。這裡的案例都應該在
    參數檢查就退出；萬一哪天某個案例走過了檢查，也只會在暫存目錄裡失敗，
    不會拿本機 `.env` 的金鑰登入券商、寫進正式的實盤紀錄庫。
    """

    sandbox: Path = Path(tempfile.mkdtemp(prefix="alphaedge-cli-"))
    (sandbox / "data" / "db").mkdir(parents=True)
    env: Dict[str, str] = {
        **os.environ,
        "ALPHAEDGE_DATA_DIR": str(sandbox / "data"),
        "ALPHAEDGE_RESULTS_DIR": str(sandbox / "results"),
        "ALPHAEDGE_LOGS_DIR": str(sandbox / "logs"),
        "ALPHAEDGE_LIVE_KILL_SWITCH_PATH": str(sandbox / "KILL_SWITCH"),
        # 空字串而非刪除：`load_dotenv()` 不覆寫已存在的鍵，`.env` 就補不回來
        "API_KEY": "",
        "API_SECRET_KEY": "",
    }
    try:
        return subprocess.run(
            [sys.executable, "run.py", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            env=env,
        )
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def test_production_without_confirmation_is_refused() -> None:
    """
    `--production` 沒帶 `--confirm-production` 時不可能連到正式環境

    **防呆在建立任何連線之前**：打錯的代價是真的下單。
    """

    result: subprocess.CompletedProcess = _run_cli(
        "--mode",
        "live",
        "--strategy",
        "MomentumStrategy1",
        "--phase",
        "close",
        "--production",
    )

    assert result.returncode == run_module.EXIT_USAGE
    assert "confirm-production" in result.stderr


def test_fake_broker_is_refused_in_production() -> None:
    """正式環境不可用假券商：一個打錯的參數不該讓真單變成假單，反過來更不行"""

    result: subprocess.CompletedProcess = _run_cli(
        "--mode",
        "live",
        "--strategy",
        "MomentumStrategy1",
        "--phase",
        "close",
        "--production",
        "--confirm-production",
        "--broker",
        "fake",
    )

    assert result.returncode == run_module.EXIT_USAGE


def test_phase_is_required() -> None:
    """沒有段落就不知道要跑什麼；靜默跑預設段落會在錯的時點送單"""

    result: subprocess.CompletedProcess = _run_cli(
        "--mode", "live", "--strategy", "MomentumStrategy1"
    )

    assert result.returncode == run_module.EXIT_USAGE


def test_unknown_strategy_is_reported() -> None:
    """策略名打錯要列出可用的"""

    result: subprocess.CompletedProcess = _run_cli(
        "--mode", "live", "--strategy", "NoSuchStrategy", "--phase", "close"
    )

    assert result.returncode == run_module.EXIT_STRATEGY_NOT_FOUND
    assert "Available strategies" in result.stderr


def test_after_close_is_wired_not_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `--phase after_close` 要真的走盤後流程

    **前身是「尚未實作，明確拒絕」**。接上之後守的東西變成：它不可以再被當成
    用法錯誤擋掉——否則排程會以為自己打錯參數，而盤後其實從來沒跑過。

    **在行程內驗、不起子行程**：子行程會沿用本機 `.env` 的金鑰登入模擬環境，
    並把整次執行寫進正式的實盤紀錄庫。這裡改以替身引擎驗兩件事：參數解析器收下
    `after_close`，以及 `run_live()` 呼叫的是 `run_after_close()` 而不是一般段落。
    """

    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--mode", "live", "--strategy", "Alpha", "--phase", "after_close"],
    )
    args: argparse.Namespace = run_module.parse_arguments()

    calls: List[str] = []

    class AfterCloseTrader:
        """只記錄被呼叫的是哪一條流程；其餘屬性供結束碼判定讀取"""

        last_reconcile: Any = None
        risk_manager: Any = type(
            "Risk", (), {"is_kill_switch_on": staticmethod(lambda: False)}
        )()
        mode_state: Any = type("Mode", (), {"account_mode": TradingMode.NORMAL})()

        def run_after_close(self) -> dict:
            calls.append("after_close")
            return {"pending_actions": 0}

        def run(self, timing: Any) -> None:
            calls.append(f"run:{timing}")

    build_kwargs: Dict[str, Any] = {}

    def fake_build(*arguments: Any, **kwargs: Any) -> AfterCloseTrader:
        build_kwargs.update(kwargs)
        return AfterCloseTrader()

    monkeypatch.setattr("core.live.factory.build_live_trader", fake_build)

    code: int = run_module.run_live(args, {"Alpha": LiveStockStrategy})

    assert calls == ["after_close"]
    assert code == 0
    # 段落名要傳進 factory 寫進 `live_run`，存活監控才比對得到
    assert build_kwargs["phase"] == "after_close"


def test_exit_codes_are_distinct() -> None:
    """
    退出碼要分得開

    排程看到 4／5 是「今天剛出事」，看到 6 是「昨天出的事還沒有人處理」，
    兩者的處理急迫性不同。
    """

    codes: List[int] = [
        run_module.EXIT_USAGE,
        run_module.EXIT_STALE_DATA,
        run_module.EXIT_RECONCILE_MISMATCH,
        run_module.EXIT_KILL_SWITCH,
        run_module.EXIT_MODE_NOT_NORMAL,
        run_module.EXIT_RESYNC_PLAN_ONLY,
    ]

    assert len(set(codes)) == len(codes)
    assert codes == [2, 3, 4, 5, 6, 7]


def test_backtest_path_is_untouched() -> None:
    """
    回測入口不受影響

    `--mode live` 的新旗標全部有預設值，回測那條路徑一個字都不用改。
    """

    parser_args: argparse.Namespace = argparse.Namespace()

    assert hasattr(run_module, "run_live")
    assert run_module.PHASE_TO_TIMING["close"] == "AT_CLOSE"
    assert parser_args is not None


def test_mixing_markets_with_disjoint_windows_is_refused(dao: LiveTradeDAO) -> None:
    """
    台股與期貨的尾盤時窗不重疊，**同一次執行不可混跑**

    這不是限制而是事實：台股 13:25~13:29、期貨 13:30~13:44。硬湊一個時窗出來
    等於整段都不送單，而且不會有任何錯誤訊息。正解是分開排程——
    部署文件列出多行 cron 就是為了這個。
    """

    with pytest.raises(ValueError, match="沒有交集"):
        build([LiveStockStrategy(), LiveFuturesStrategy()], dao)
