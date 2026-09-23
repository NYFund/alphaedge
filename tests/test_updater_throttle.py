import datetime
import time
from typing import List, Optional, Tuple

import pytest

from core.pipeline.shared.base_updater import (
    BaseDataUpdater,
    DailyTwoMarketUpdater,
)
from core.pipeline.shared.date_planner import DateProgressStore
from core.pipeline.shared.graceful_stop import GracefulStop
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
)
from core.pipeline.tw.updaters.futures_chip_updater import FuturesChipUpdater
from core.pipeline.tw.updaters.futures_price_updater import FuturesPriceUpdater
from core.pipeline.tw.updaters.monthly_revenue_report_updater import (
    MonthlyRevenueReportUpdater,
)
from core.pipeline.tw.updaters.stock_chip_updater import StockChipUpdater
from core.pipeline.tw.updaters.stock_margin_updater import StockMarginUpdater
from core.pipeline.tw.updaters.stock_price_updater import StockPriceUpdater

"""
長跑 updater 的節流與中止行為

**這裡釘住的是「按了 Ctrl+C 要有反應」**。`time.sleep()` 被訊號打斷會自動續睡
（PEP 475），所以「每 100 天睡 2 分鐘」那一段用裸 sleep 的話，按下 Ctrl+C
得等滿 2 分鐘。數十小時的回補按了沒反應，實際上就是逼人用 `kill -9`——
而那會讓手上尚未入庫的那批直接消失，下次重跑還得重爬。

節流秒數本身不測（那是對站方的禮貌，不是正確性），測的是
**中止旗標立起來之後多久會回來**。
"""


# 中止後允許的最長反應時間；`GracefulStop.SLEEP_SLICE_SECONDS` 是 0.2 秒，
# 留幾倍餘裕以免在負載高的 CI 上偶發失敗
MAX_STOP_LATENCY_SECONDS: float = 1.0


class _ThrottleOnly(BaseDataUpdater):
    """只用來驗節流的最小 updater"""

    BATCH_SLEEP_EVERY_N_FILES: int = 3
    BATCH_SLEEP_DURATION_SECONDS: int = 30
    BATCH_RANDOM_DELAY_MIN: int = 30
    BATCH_RANDOM_DELAY_MAX: int = 30

    def setup(self) -> None:
        """不需要設定"""

    def update(self) -> None:
        """不需要實作"""


# === 可中斷節流 ===
def test_long_sleep_returns_immediately_once_stop_is_requested() -> None:
    """
    中止旗標立起來後，長睡那一段要立刻回來

    這是本步驟的核心：改動前走的是 `time.sleep(120)`，按 Ctrl+C 要等滿 120 秒。
    """

    updater: _ThrottleOnly = _ThrottleOnly()
    stop: GracefulStop = GracefulStop(label="test")
    stop.request(reason="test")

    started: float = time.monotonic()
    file_cnt: int = updater.throttle_per_file(updater.BATCH_SLEEP_EVERY_N_FILES, stop)
    elapsed: float = time.monotonic() - started

    assert elapsed < MAX_STOP_LATENCY_SECONDS, f"長睡耗了 {elapsed:.2f} 秒才回來"
    assert file_cnt == 0, "達到門檻時計數要歸零"


def test_short_sleep_returns_immediately_once_stop_is_requested() -> None:
    """隨機短睡那一段同樣要能被中止打斷"""

    updater: _ThrottleOnly = _ThrottleOnly()
    stop: GracefulStop = GracefulStop(label="test")
    stop.request(reason="test")

    started: float = time.monotonic()
    file_cnt: int = updater.throttle_per_file(1, stop)
    elapsed: float = time.monotonic() - started

    assert elapsed < MAX_STOP_LATENCY_SECONDS, f"短睡耗了 {elapsed:.2f} 秒才回來"
    assert file_cnt == 1, "未達門檻時計數要維持不變"


def test_throttle_without_stop_still_sleeps() -> None:
    """
    沒傳 `stop` 時仍然照睡

    節流的目的是對站方限流的禮貌，不可因為「沒有中止旗標」就變成不睡——
    那會讓沒接上 `GracefulStop` 的呼叫端在回補時被站方擋下來。
    """

    class _Quick(_ThrottleOnly):
        BATCH_SLEEP_EVERY_N_FILES: int = 1
        BATCH_SLEEP_DURATION_SECONDS: int = 1

    started: float = time.monotonic()
    _Quick().throttle_per_file(1, None)
    elapsed: float = time.monotonic() - started

    assert elapsed >= 1.0, f"沒傳 stop 時應照睡滿，實際只有 {elapsed:.2f} 秒"


def test_counter_resets_only_at_the_threshold() -> None:
    """計數只在達到門檻時歸零，否則原樣傳回"""

    updater: _ThrottleOnly = _ThrottleOnly()
    # 三個秒數都歸零：本測試只驗計數的進位與歸零，不驗真的睡了多久
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.BATCH_SLEEP_DURATION_SECONDS = 0

    assert updater.throttle_per_file(0) == 0
    assert updater.throttle_per_file(1) == 1
    assert updater.throttle_per_file(2) == 2
    assert updater.throttle_per_file(3) == 0, "達到 BATCH_SLEEP_EVERY_N_FILES 時要歸零"
    assert updater.throttle_per_file(4) == 0, (
        "超過門檻同樣歸零（防止計數溢出後永遠不睡）"
    )


# === 三支日頻 updater 的骨架 ===
DAILY_UPDATERS: List[type] = [
    StockPriceUpdater,
    StockChipUpdater,
    StockMarginUpdater,
]


@pytest.mark.parametrize("updater_cls", DAILY_UPDATERS)
def test_daily_updaters_share_the_template_method(updater_cls: type) -> None:
    """
    三支都走同一份 `update()`，不可有人自己覆寫一份

    覆寫等於回到「各抄一份 100 行迴圈」的狀態，而那正是本次收斂要消滅的東西。
    """

    assert issubclass(updater_cls, DailyTwoMarketUpdater)
    assert "update" not in updater_cls.__dict__, (
        f"{updater_cls.__name__} 自行覆寫了 update()，骨架又被抄了一份"
    )


@pytest.mark.parametrize("updater_cls", DAILY_UPDATERS)
def test_daily_updaters_declare_their_source(updater_cls: type) -> None:
    """
    `SOURCE` 同時決定進度檔名與 crawler／cleaner 的方法名

    漏填會讓進度檔寫到 `_date_progress.json`、`getattr` 也找不到方法，
    但那要跑到才會發現。
    """

    assert updater_cls.SOURCE, f"{updater_cls.__name__} 沒有填 SOURCE"
    assert updater_cls.LOG_FILE_NAME.endswith(".log")
    assert hasattr(updater_cls, "plan_dates")


@pytest.mark.parametrize("updater_cls", DAILY_UPDATERS)
def test_each_daily_updater_plans_its_own_dates(updater_cls: type) -> None:
    """
    `plan_dates()` 必須各自實作

    三支的日曆來源不同（chip／margin 以 price 表為日曆，price 只能以平日為母集合
    再補回補行交易日）。共用一份會讓某一支靜靜用錯日曆——症狀是整天漏抓，不報錯。
    """

    assert "plan_dates" in updater_cls.__dict__, (
        f"{updater_cls.__name__} 沒有自己的 plan_dates()"
    )


def test_base_template_cannot_be_instantiated_without_plan_dates() -> None:
    """`plan_dates()` 是抽象方法，漏實作要在建構時就炸，不是跑到一半"""

    class _NoPlan(DailyTwoMarketUpdater):
        SOURCE: str = "demo"

    with pytest.raises(TypeError, match="plan_dates"):
        _NoPlan()


@pytest.mark.parametrize("updater_cls", DAILY_UPDATERS)
def test_source_resolves_to_real_crawler_and_cleaner_methods(
    updater_cls: type,
) -> None:
    """
    `SOURCE` 組出來的方法名必須真的存在於 crawler／cleaner 上

    骨架是以 `getattr(self.crawler, f"crawl_twse_{SOURCE}")` 取方法的，
    **少了那個方法不會在 import 時報錯，要跑到當天才炸**——而 ETL 是排程跑的，
    炸在凌晨沒人看著。單元測試用的是 `SimpleNamespace` 替身，替身照著同樣的規則
    命名，所以替身永遠對得上；真正會漂的是這裡的正式類別。
    """

    source: str = updater_cls.SOURCE
    crawler_cls: type = _annotated_type(updater_cls, "crawler")
    cleaner_cls: type = _annotated_type(updater_cls, "cleaner")

    for method in (f"crawl_twse_{source}", f"crawl_tpex_{source}"):
        assert hasattr(crawler_cls, method), (
            f"{crawler_cls.__name__} 沒有 {method}()，骨架的 getattr 會在跑到當天才炸"
        )

    for method in (f"clean_twse_{source}", f"clean_tpex_{source}"):
        assert hasattr(cleaner_cls, method), (
            f"{cleaner_cls.__name__} 沒有 {method}()，骨架的 getattr 會在跑到當天才炸"
        )


def _annotated_type(updater_cls: type, attribute: str) -> type:
    """
    取出 updater 在 `__init__` 內對 `self.<attribute>` 標註的型別

    **不能用 `typing.get_type_hints()`**：它只看得到函式簽名的標註，
    看不到函式體內 `self.crawler: StockChipCrawler = ...` 這種寫法。
    也不建構 updater——那會開啟正式資料庫連線。
    """

    import ast
    import importlib
    import inspect

    tree: ast.Module = ast.parse(inspect.getsource(inspect.getmodule(updater_cls)))
    module = importlib.import_module(updater_cls.__module__)

    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(
            node.target, ast.Attribute
        ):
            continue
        if node.target.attr != attribute:
            continue
        return getattr(module, ast.unparse(node.annotation))

    raise AssertionError(
        f"{updater_cls.__name__} 的 __init__ 沒有標註 self.{attribute}"
    )


def test_only_price_overrides_the_row_threshold() -> None:
    """
    原始列數門檻是 price 專屬的 hook，不可被硬統一到基底

    chip／margin 的來源表沒有「只剩表頭或合計列」這種版面，套上去只會誤殺。
    """

    assert "clean_day" in StockPriceUpdater.__dict__
    assert "clean_day" not in StockChipUpdater.__dict__
    assert "clean_day" not in StockMarginUpdater.__dict__
    assert StockPriceUpdater.MIN_DF_ROWS_AFTER_CLEAN > 0


# === 長跑 updater 都接上可中斷節流 ===
LONG_RUNNING_UPDATERS: List[type] = [
    StockPriceUpdater,
    StockChipUpdater,
    StockMarginUpdater,
    FuturesPriceUpdater,
    MonthlyRevenueReportUpdater,
    # 這兩支一開始漏列，於是它們各自留著一份裸 sleep 沒被發現。
    # **清單漏一支，那一支就等於沒有守門**，而「哪幾支有守門」看不出來
    FuturesChipUpdater,
    FinancialStatementUpdater,
]

# **刻意不列**（2026-09-23 的排程實測時間）：
#   dividend 約 3 分、corporate_action 約 1.7 分、futures_margin 約 3 秒。
# 它們也有裸 `time.sleep()`，但整段就跑幾分鐘——「按 Ctrl+C 要等幾秒」在那個
# 量級不構成問題，而本檔的守門是為了「數小時～數十小時的回補按了沒反應」。
# 哪天它們變長了再列進來；列進來就會因為裸 sleep 而變紅，那正是預期行為。


@pytest.mark.parametrize("updater_cls", LONG_RUNNING_UPDATERS)
def test_long_running_updaters_do_not_shadow_the_shared_throttle(
    updater_cls: type,
) -> None:
    """
    沒有人可以覆寫基底的 `throttle_per_file()`

    子類**可以**有自己的節流（單位不同就該分開，例如逐請求、逐月批次），
    但不可以用同一個名字——簽名不同的同名方法會把基底那份遮蔽掉，
    而遮蔽不會報錯，只會在某天有人呼叫基底那份時把參數綁到錯的位置上。
    """

    assert hasattr(updater_cls, "throttle_per_file")
    assert updater_cls.throttle_per_file is BaseDataUpdater.throttle_per_file, (
        f"{updater_cls.__name__} 覆寫了 throttle_per_file()"
    )


@pytest.mark.parametrize("updater_cls", LONG_RUNNING_UPDATERS)
def test_no_updater_keeps_a_bare_sleep(updater_cls: type) -> None:
    """
    長跑 updater 的節流一律走可中斷的 sleep，不可自己寫裸 `time.sleep()`

    自己寫一份的代價不是行數，而是那一支按 Ctrl+C 沒有反應——
    而「哪幾支有反應」是看不出來的，只有真的去按才知道。
    `FuturesChipUpdater` 就是這樣留了一份裸 sleep 直到守門清單補齊才被發現。
    """

    import ast
    import inspect

    # **以 AST 掃而不是字串比對**：註解與 docstring 裡本來就會提到
    # `time.sleep()`（說明為什麼不用它），字串比對會把那些說明當成違規
    tree: ast.Module = ast.parse(inspect.getsource(inspect.getmodule(updater_cls)))
    offenders: List[str] = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "sleep"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
    ]

    assert offenders == [], (
        f"{updater_cls.__name__} 所在模組仍有裸 time.sleep()：{offenders}"
    )


# === 骨架的收尾順序 ===
def test_interrupted_run_still_loads_and_saves() -> None:
    """
    中途被要求收工時，手上那批仍要入庫、進度仍要存檔

    這是 `GracefulStop` 存在的理由：預設的 `KeyboardInterrupt` 從當下那一行炸出去，
    已爬好、還沒湊滿一批的日期全部作廢，而那些日期在資料庫裡不留任何列，
    下次重跑會被當成「還沒爬」再打一次。
    """

    loaded: List[List[str]] = []
    saved: List[bool] = []

    class _Interrupting(DailyTwoMarketUpdater):
        SOURCE: str = "demo"
        SOURCE_LABEL: str = "Demo"
        LOG_FILE_NAME: str = "demo.log"
        BATCH_RANDOM_DELAY_MIN: int = 0
        BATCH_RANDOM_DELAY_MAX: int = 0

        def __init__(self, dates: List[datetime.date]) -> None:
            super().__init__()
            self._dates: List[datetime.date] = dates
            self.stop: Optional[GracefulStop] = None

        def setup(self) -> None:
            """測試不設定 log"""

        def plan_dates(
            self,
            progress: DateProgressStore,
            start_date: datetime.date,
            end_date: datetime.date,
        ) -> List[datetime.date]:
            """直接回傳腳本給定的日期"""

            return self._dates

        def crawl_day(self, date: datetime.date) -> Tuple[object, object]:
            """第二天開始要求收工，模擬使用者在爬取途中按下 Ctrl+C"""

            if date == self._dates[1]:
                self._stop.request(reason="test")
            return _ok_result(), _ok_result()

        def clean_day(self, date, twse, tpex) -> bool:
            """測試不清洗"""

            return True

        def load_batch(self, batch_dates: List[str]) -> None:
            """記下每次入庫的日期"""

            loaded.append(list(batch_dates))

        def report_latest_date(self) -> None:
            """測試不查表"""

    pytest.importorskip("pandas")
    dates: List[datetime.date] = [
        datetime.date(2024, 1, 2),
        datetime.date(2024, 1, 3),
        datetime.date(2024, 1, 4),
    ]

    updater: _Interrupting = _Interrupting(dates)
    _run_with_captured_stop(updater, dates, saved)

    assert loaded == [["20240102", "20240103"]], (
        "中止時手上那批（含觸發中止的那一天）仍要入庫"
    )
    assert saved == [True], "進度必須存檔，否則已確認過的日子下次會重問"


def _ok_result():
    """最小可用的成功爬取結果"""

    import pandas as pd

    from core.pipeline.shared.base_crawler import CrawlResult, CrawlStatus

    return CrawlResult(status=CrawlStatus.OK, data=pd.DataFrame({"a": [1]}))


def _run_with_captured_stop(
    updater: DailyTwoMarketUpdater,
    dates: List[datetime.date],
    saved: List[bool],
) -> None:
    """跑一次 `update()`，並把骨架建立的 `GracefulStop` 與進度存檔攔下來"""

    import core.pipeline.shared.base_updater as base_updater_module

    real_stop_cls = base_updater_module.GracefulStop

    def capture(*args, **kwargs):
        instance = real_stop_cls(*args, **kwargs)
        updater._stop = instance
        return instance

    class _Progress:
        no_data: set = set()
        incomplete: set = set()

        def record(self, date, status) -> None:
            """測試不記錄"""

        def save(self) -> None:
            """記下有沒有存檔"""

            saved.append(True)

    original_stop = base_updater_module.GracefulStop
    original_progress = base_updater_module.DateProgressStore
    base_updater_module.GracefulStop = capture
    base_updater_module.DateProgressStore = lambda source: _Progress()
    try:
        updater.update(start_date=dates[0], end_date=dates[-1])
    finally:
        base_updater_module.GracefulStop = original_stop
        base_updater_module.DateProgressStore = original_progress
