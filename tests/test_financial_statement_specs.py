from pathlib import Path
from typing import List

import pytest

from core.config import (
    BALANCE_SHEET_TABLE_NAME,
    CASH_FLOW_TABLE_NAME,
    COMPREHENSIVE_INCOME_TABLE_NAME,
)
from core.pipeline.shared.base_updater import BaseDataUpdater
from core.pipeline.tw.updaters.financial_statement import (
    EquityChangeMixin,
    EquityChangeSeasonStats,
)
from core.pipeline.tw.updaters.financial_statement_updater import (
    FinancialStatementUpdater,
    StatementSpec,
)

"""
財報 updater 拆分後的結構契約

三張「全市場一次查完」的報表原本是三份各 60 行的複本，正規化後 diff 只剩
docstring 與 log 標籤。合併成一份 `update_statement(spec)` 之後，這裡釘住兩件
**壞掉不會報錯**的事：

1. **log 標籤要逐字不變**。回補時 log 裡那幾行是判斷「現在跑到哪張表」的唯一
   依據，大小寫改掉不會有任何測試失敗，但翻 log 的人會對不上。
2. **spec 每次重建**。`crawl`／`clean` 綁的是 `self.crawler` 的繫結方法，
   快取成類別常數的話，測試換掉 crawler 之後仍會綁到舊的那一個。
"""


# 改動前三張報表各自寫死的 log 字串，逐字抄下來當基準
EXPECTED_LABELS: List[tuple] = [
    (BALANCE_SHEET_TABLE_NAME, "Balance Sheet", "Balance sheet", "balance sheet"),
    (
        COMPREHENSIVE_INCOME_TABLE_NAME,
        "Comprehensive Income",
        "Comprehensive income",
        "comprehensive income",
    ),
    (CASH_FLOW_TABLE_NAME, "Cash Flow", "Cash flow", "cash flow"),
]


@pytest.fixture
def updater() -> FinancialStatementUpdater:
    """不連資料庫、不建 ETL 元件的 updater；只填 spec 需要的屬性"""

    instance: FinancialStatementUpdater = FinancialStatementUpdater.__new__(
        FinancialStatementUpdater
    )
    instance.crawler = object()
    instance.cleaner = object()
    base: Path = Path("/tmp/fs")
    instance.balance_sheet_dir = base / "balance_sheet"
    instance.comprehensive_income_dir = base / "comprehensive_income"
    instance.cash_flow_dir = base / "cash_flow"
    return instance


class _Recorder:
    """任何 `crawl_*`／`clean_*` 都回傳一個可辨識的物件"""

    def __init__(self, tag: str) -> None:
        self.tag: str = tag

    def __getattr__(self, name: str) -> str:
        return f"{self.tag}:{name}"


# === log 標籤逐字不變 ===
def test_spec_labels_match_the_original_log_strings(
    updater: FinancialStatementUpdater,
) -> None:
    """三種大小寫形式都要與合併前寫死的字串逐字相同"""

    updater.crawler = _Recorder("crawler")
    updater.cleaner = _Recorder("cleaner")
    specs: List[StatementSpec] = updater.statement_specs()

    assert len(specs) == 3, "三張全市場報表都要在清單裡，少一張就是整張表不再更新"

    for spec, (table, label, log_label, lowercase) in zip(specs, EXPECTED_LABELS):
        assert spec.table_name == table
        assert spec.label == label, "「* Start Updating {label} Data...」用的是這個"
        assert spec.log_label == log_label, "`log_latest_year_season()` 用的是這個"
        assert spec.lowercase_label == lowercase, (
            "「Cleaned {x} dataframe empty」用這個"
        )


def test_spec_order_is_the_update_order(updater: FinancialStatementUpdater) -> None:
    """
    順序即更新順序，且權益變動表不在其中

    權益變動表是逐檔查、量級差三個數量級，混進這份清單會讓它跟著跑
    「全市場一次查完」的流程——而那條流程沒有節流、沒有分批、不可中斷。
    """

    updater.crawler = _Recorder("crawler")
    updater.cleaner = _Recorder("cleaner")
    tables: List[str] = [spec.table_name for spec in updater.statement_specs()]

    assert tables == [
        BALANCE_SHEET_TABLE_NAME,
        COMPREHENSIVE_INCOME_TABLE_NAME,
        CASH_FLOW_TABLE_NAME,
    ]
    assert "equity_change" not in tables


# === spec 每次重建 ===
def test_specs_are_rebuilt_on_every_call(updater: FinancialStatementUpdater) -> None:
    """
    換掉 crawler 之後，spec 必須綁到新的那一個

    快取成類別常數的話這裡會拿到舊的繫結方法，而症狀是「測試替身明明換了卻
    沒有生效」——測試照樣綠，只是測的是別人。
    """

    updater.crawler = _Recorder("first")
    updater.cleaner = _Recorder("first")
    before: StatementSpec = updater.statement_specs()[0]

    updater.crawler = _Recorder("second")
    updater.cleaner = _Recorder("second")
    after: StatementSpec = updater.statement_specs()[0]

    assert before.crawl == "first:crawl_balance_sheet"
    assert after.crawl == "second:crawl_balance_sheet"
    assert before.clean == "first:clean_balance_sheet"
    assert after.clean == "second:clean_balance_sheet"


def test_each_spec_points_at_its_own_directory(
    updater: FinancialStatementUpdater,
) -> None:
    """
    三張報表的落地目錄不可互指

    指錯不會報錯，只會把某張表的 CSV 載進另一張表——而 `INSERT OR IGNORE`
    會讓多數列靜靜被忽略，看起來像「沒有新資料」。
    """

    updater.crawler = _Recorder("crawler")
    updater.cleaner = _Recorder("cleaner")
    dirs: List[Path] = [spec.dir_path for spec in updater.statement_specs()]

    assert len(set(dirs)) == 3
    assert dirs[0].name == "balance_sheet"
    assert dirs[1].name == "comprehensive_income"
    assert dirs[2].name == "cash_flow"


# === 拆分後的結構 ===
def test_updater_still_exposes_the_equity_change_constants() -> None:
    """
    `EQUITY_CHANGE_*` 仍要在 `FinancialStatementUpdater` 上解析得到

    測試是用 `monkeypatch.setattr(FinancialStatementUpdater, "EQUITY_CHANGE_...", 0)`
    把節流關掉的。搬成獨立元件的話那些 setattr 會設在一個沒人讀的類別上，
    **每一條測試都會真的睡滿節流時間**，而且不會有任何失敗。
    """

    for name in (
        "EQUITY_CHANGE_LOAD_BATCH_SIZE",
        "EQUITY_CHANGE_RANDOM_DELAY_MIN",
        "EQUITY_CHANGE_RANDOM_DELAY_MAX",
        "EQUITY_CHANGE_BATCH_SLEEP_EVERY_N_FILES",
        "EQUITY_CHANGE_BATCH_SLEEP_DURATION_SECONDS",
        "EQUITY_CHANGE_PROBE_STOCK_IDS",
        "EQUITY_CHANGE_PROGRESS_SAVE_EVERY_N_FILES",
        "EQUITY_CHANGE_MAX_CONSECUTIVE_ERRORS",
    ):
        assert hasattr(FinancialStatementUpdater, name), (
            f"{name} 解析不到，測試裡關節流的 monkeypatch 會靜靜失效"
        )

    assert issubclass(FinancialStatementUpdater, EquityChangeMixin)
    assert EquityChangeSeasonStats is not None


def test_equity_change_throttle_does_not_shadow_the_shared_one() -> None:
    """
    權益變動表的節流不可與基底同名

    它的單位是**請求**、簽名是 `(stop, cnt)`，而基底那份的單位是**檔案／日期**、
    簽名是 `(file_cnt, stop)`。同名的話三張全市場報表呼叫基底那份時會把
    `file_cnt` 綁到 `stop` 參數上——拆分前正是這個狀態，而它之所以沒發作，
    只是因為當時那三張報表各自寫了一份裸 sleep、沒人呼叫共用的那個。
    現在兩者的名字都帶單位：`throttle_per_file` 與 `throttle_per_request`。
    """

    assert hasattr(FinancialStatementUpdater, "throttle_per_request")
    assert (
        FinancialStatementUpdater.throttle_per_file is BaseDataUpdater.throttle_per_file
    ), "`throttle_per_file` 被覆寫了，三張全市場報表的節流會綁錯參數"
