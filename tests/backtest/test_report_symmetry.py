import ast
import inspect
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Optional, Set

import pandas as pd
import pytest

import core.backtest.backtester as backtester_module
import core.backtest.models.fill_model as fill_model_module
import core.backtest.models.settlement_model.tw_futures as tw_futures_module
import core.backtest.models.settlement_model.tw_stock as tw_stock_module
import core.backtest.simulator.bar_simulator as bar_simulator_module
import core.backtest.simulator.base as simulator_base_module
import core.portfolio.order_rules as order_rules_module
import core.position.stock.position_manager as stock_position_module
from core.backtest.models.event_counts import (
    EVENT_KEYS,
    FUTURES_EVENT_KEYS,
    FUTURES_ONLY_EVENT_KEYS,
    SHARED_EVENT_KEYS,
    STOCK_EVENT_KEYS,
    TICK_EVENT_KEYS,
    new_event_counts,
)
from core.backtest.report.base import BaseBacktestReporter
from core.backtest.report.futures_reporter import FuturesBacktestReporter
from core.backtest.report.reporter import StockBacktestReporter

"""
回測報表與事件計數的對稱性：台股與期貨只在市場本質處不同

- 兩支 reporter 都直接繼承 `BaseBacktestReporter`，共用的迴圈（交易明細、多空統計、
  指標、權益口徑）不可在子類再抄一份——抄一份就會漂移。
- 事件 key 分「共用／台股／期貨」：期貨的事件報表不再列出二十個永遠是 0 的台股列；
  台股清單的內容與順序維持不變（事件報表與 SHORT 回歸基準以它為欄位）。
- 各市場程式碼實際計入的 key 必須在該市場的清單裡，否則 `event_counts[key] += 1`
  會在跑到那個事件時才 KeyError。
"""

# 台股事件報表的順序（2026-10 以前的 `EVENT_KEYS` 原文）；動到這裡就是破壞報表相容
LEGACY_STOCK_EVENT_KEYS: List[str] = [
    "rejected_direction",
    "rejected_fill_price",
    "fill_price_clamped",
    "close_price_out_of_range",
    "forced_cover_day_trade",
    "forced_cover_margin_call",
    "forced_cover_insufficient_margin",
    "forced_cover_max_holding",
    "forced_cover_suspended",
    "forced_cover_no_quote",
    "limit_up_cover_failed",
    "rejected_max_holdings",
    "rejected_no_quote",
    "rejected_insufficient_balance",
    "rejected_no_borrow",
    "rejected_short_suspended",
    "rejected_short_halted",
    "rejected_below_reference",
    "rejected_not_day_tradable",
    "rejected_limit_up_locked",
    "rejected_limit_down_locked",
    "rejected_volume_cap",
    "truncated_by_volume",
    "dividend_compensation_paid",
    "dividend_compensation_unknown",
    "dividend_received",
    "share_adjustment_applied",
    "share_adjustment_unknown",
    "forced_exit_no_quote",
]

# 市場無關、兩邊都會計入的程式碼
SHARED_MODULES: List[ModuleType] = [
    backtester_module,
    simulator_base_module,
    bar_simulator_module,
    order_rules_module,
]


# === 事件 key 的分組 ===
def test_stock_event_keys_keep_their_order() -> None:
    """台股清單的內容與順序不可動：事件報表與 SHORT 回歸基準都以它為欄位"""

    assert list(EVENT_KEYS) == LEGACY_STOCK_EVENT_KEYS
    assert STOCK_EVENT_KEYS is EVENT_KEYS
    assert list(new_event_counts()) == LEGACY_STOCK_EVENT_KEYS


def test_shared_keys_are_a_subset_of_the_stock_list_in_order() -> None:
    """共用 key 全部來自台股清單，且維持相對順序"""

    assert set(SHARED_EVENT_KEYS) < set(EVENT_KEYS)
    assert [key for key in EVENT_KEYS if key in SHARED_EVENT_KEYS] == list(
        SHARED_EVENT_KEYS
    )


def test_futures_keys_are_shared_plus_futures_only() -> None:
    """期貨清單＝共用＋期貨專屬；不含任何台股專屬 key，也不含逐筆專屬 key"""

    assert FUTURES_EVENT_KEYS == SHARED_EVENT_KEYS + FUTURES_ONLY_EVENT_KEYS
    assert "rolled_contract" in FUTURES_ONLY_EVENT_KEYS
    assert not (set(FUTURES_ONLY_EVENT_KEYS) & set(EVENT_KEYS))
    assert not (set(TICK_EVENT_KEYS) & set(FUTURES_EVENT_KEYS))
    assert not (set(TICK_EVENT_KEYS) & set(EVENT_KEYS))
    assert list(new_event_counts(FUTURES_EVENT_KEYS)) == list(FUTURES_EVENT_KEYS)


def _event_keys_used(
    module: ModuleType, class_names: Optional[Set[str]] = None
) -> Set[str]:
    """
    以 AST 掃出模組（或其中幾個 class）實際計入的事件 key

    認三種寫法：`<x>.event_counts["key"]`、`_count(event_counts, "key")`、
    `event_counts.get("key", …)`。以變數當 key 的寫法掃不到，由各市場的
    端到端測試補。
    """

    tree: ast.Module = ast.parse(inspect.getsource(module))
    roots: List[ast.AST] = (
        list(tree.body)
        if class_names is None
        else [
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name in class_names
        ]
    )
    if class_names is not None:
        assert len(roots) == len(class_names), f"{module.__name__} 找不到 {class_names}"

    def owner_name(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return ""

    keys: Set[str] = set()
    for root in roots:
        for node in ast.walk(root):
            if (
                isinstance(node, ast.Subscript)
                and owner_name(node.value) == "event_counts"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys.add(node.slice.value)
            if isinstance(node, ast.Call):
                func: ast.AST = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "_count"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                ):
                    keys.add(node.args[1].value)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and owner_name(func.value) == "event_counts"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                ):
                    keys.add(node.args[0].value)
    return keys


def test_every_key_the_futures_side_counts_is_in_the_futures_list() -> None:
    """期貨程式碼計入的每個 key 都在期貨清單裡，否則跑到那個事件才 KeyError"""

    used: Set[str] = set()
    for module in SHARED_MODULES:
        used |= _event_keys_used(module)
    used |= _event_keys_used(fill_model_module, {"BaseFillModel", "TwFuturesFillModel"})
    used |= _event_keys_used(tw_futures_module)

    assert used, "一個 key 都掃不到，掃描規則與程式碼的寫法對不上"
    assert used <= set(FUTURES_EVENT_KEYS), (
        f"期貨側計入但不在 FUTURES_EVENT_KEYS：{sorted(used - set(FUTURES_EVENT_KEYS))}"
    )
    # 反向也要成立：標成共用的 key 期貨側一定計得到，否則它只是台股專屬卻混進期貨報表
    assert set(SHARED_EVENT_KEYS) <= used, (
        f"標成共用但期貨側不會計入：{sorted(set(SHARED_EVENT_KEYS) - used)}"
    )


def test_every_key_the_stock_side_counts_is_in_the_stock_list() -> None:
    """台股程式碼計入的每個 key 都在台股清單裡（同一條守門，另一個市場）"""

    used: Set[str] = set()
    for module in SHARED_MODULES:
        used |= _event_keys_used(module)
    used |= _event_keys_used(fill_model_module, {"BaseFillModel", "TwStockFillModel"})
    used |= _event_keys_used(tw_stock_module)
    used |= _event_keys_used(stock_position_module)

    assert used <= set(EVENT_KEYS), (
        f"台股側計入但不在 EVENT_KEYS：{sorted(used - set(EVENT_KEYS))}"
    )


def test_shared_code_only_counts_shared_keys() -> None:
    """市場無關的程式碼只能計入共用 key；計入某一市場專屬的 key 就是市場語意洩漏"""

    used: Set[str] = set()
    for module in SHARED_MODULES:
        used |= _event_keys_used(module)
    used |= _event_keys_used(fill_model_module, {"BaseFillModel"})

    assert used <= set(SHARED_EVENT_KEYS), (
        f"市場無關程式碼計入了專屬 key：{sorted(used - set(SHARED_EVENT_KEYS))}"
    )


# === reporter 的繼承與分工 ===
SHARED_REPORTER_METHODS: List[str] = [
    "generate_trading_report",
    "generate_direction_summary",
    "generate_event_report",
    "generate_metrics_summary",
    "get_equity_series",
    "sorted_closed_records",
    "save_report",
    "close",
]


def test_both_reporters_inherit_the_base_directly() -> None:
    """期貨報表不再繼承台股報表：市場差異各自實作，共用的只有基底"""

    assert issubclass(StockBacktestReporter, BaseBacktestReporter)
    assert issubclass(FuturesBacktestReporter, BaseBacktestReporter)
    assert not issubclass(FuturesBacktestReporter, StockBacktestReporter)


@pytest.mark.parametrize(
    "reporter_cls", [StockBacktestReporter, FuturesBacktestReporter]
)
def test_subclasses_do_not_copy_the_shared_loops(reporter_cls: type) -> None:
    """共用的迴圈與入口只能在基底有一份；子類覆寫等於又抄了一份"""

    copied: List[str] = [
        name for name in SHARED_REPORTER_METHODS if name in reporter_cls.__dict__
    ]
    assert copied == [], f"{reporter_cls.__name__} 覆寫了共用方法：{copied}"


@pytest.mark.parametrize(
    ("reporter_cls", "identifier"),
    [(StockBacktestReporter, "Symbol"), (FuturesBacktestReporter, "Contract ID")],
)
def test_report_columns_keep_identifier_and_cumulative_tail(
    reporter_cls: type, identifier: str
) -> None:
    """前端以識別欄名判斷報表型別，欄名不可改；累計兩欄由基底補在尾端"""

    columns = reporter_cls.TRADING_REPORT_COLUMNS
    assert columns[0] == identifier
    assert columns[-2:] == BaseBacktestReporter.CUMULATIVE_COLUMNS
    assert len(set(columns)) == len(columns)


def test_futures_event_report_lists_only_futures_keys(tmp_path: Path) -> None:
    """期貨事件報表只列期貨清單：共用 key 在前、換月在後，沒有台股的信用交易列"""

    reporter: FuturesBacktestReporter = FuturesBacktestReporter.__new__(
        FuturesBacktestReporter
    )
    reporter.output_dir = tmp_path
    reporter.strategy = type("S", (), {"strategy_name": "demo"})()

    counts: Dict[str, int] = new_event_counts(FUTURES_EVENT_KEYS)
    counts["rolled_contract"] = 3
    df: pd.DataFrame = reporter.generate_event_report(counts)

    assert df["Event"].tolist() == list(FUTURES_EVENT_KEYS)
    assert df.set_index("Event")["Count"]["rolled_contract"] == 3
    assert "rejected_no_borrow" not in df["Event"].tolist()
    assert (tmp_path / "demo_event_report.csv").exists()


def test_stock_factory_keeps_the_stock_event_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """台股 factory 建的計數器仍是原本的台股清單（事件報表與回歸基準的欄位不變）"""

    from core.backtest.backtester import Backtester
    from core.backtest.factory import build_backtester
    from strategies.stock.volume_breakout_momentum_strategy import (
        VolumeBreakoutMomentumStrategy,
    )

    monkeypatch.setattr(Backtester, "setup", lambda self: None)
    backtester: Backtester = build_backtester(
        VolumeBreakoutMomentumStrategy(), write_artifacts=False
    )

    assert list(backtester.event_counts) == LEGACY_STOCK_EVENT_KEYS
    assert backtester.fill_model.event_counts is backtester.event_counts
