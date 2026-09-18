from pathlib import Path
from typing import List

import pandas as pd
import pytest

from tests.backtest.make_short_baseline import (
    BASELINE_FILE_NAME,
    POSITION_FILE_NAME,
    SNAPSHOT_DIR,
    SUMMARY_FILE_NAME,
    build_scenarios,
    collect_position_rows,
    collect_summary_row,
    collect_trade_rows,
    run_scenario,
)

"""
SHORT 路徑回歸測試：多市場抽象重構的第二條回歸線

與 LONG 回歸的分工：
- LONG 走真實 tw_stock.db、驗的是「換了引擎後既有做多策略的結果不變」（約 54 秒）
- SHORT 全部由腳本驅動、不連 DB，驗的是「放空記帳的每一項攤提與計提不變」（秒級）
"""


def run_all_scenarios() -> tuple:
    """重跑所有情境並組出與 baseline 同結構的三份表"""

    trade_rows: List[dict] = []
    position_rows: List[dict] = []
    summary_rows: List[dict] = []

    for scenario in build_scenarios():
        backtester = run_scenario(scenario)

        trade_rows.extend(collect_trade_rows(scenario.name, backtester))
        position_rows.extend(collect_position_rows(scenario.name, backtester))
        summary_rows.append(collect_summary_row(scenario.name, backtester))

    return (
        pd.DataFrame(trade_rows),
        pd.DataFrame(position_rows),
        pd.DataFrame(summary_rows),
    )


def assert_matches_snapshot(actual: pd.DataFrame, file_name: str) -> None:
    """與指定的快照檔逐欄位比對"""

    snapshot_path: Path = SNAPSHOT_DIR / file_name
    assert snapshot_path.exists(), (
        f"{file_name} 不存在，請先執行 make_short_baseline.py"
    )

    expected: pd.DataFrame = pd.read_csv(snapshot_path, dtype={"Symbol": str})

    assert len(actual) == len(expected), f"{file_name} 的列數與 baseline 不同"
    assert list(actual.columns) == list(expected.columns), (
        f"{file_name} 的欄位組成與 baseline 不同"
    )

    pd.testing.assert_frame_equal(
        actual.reset_index(drop=True),
        expected[actual.columns].reset_index(drop=True),
        check_dtype=False,
    )


def test_short_regression_snapshot() -> None:
    """重跑六組放空情境並與 baseline 逐筆比對交易紀錄"""

    trades, _, _ = run_all_scenarios()
    assert_matches_snapshot(trades, BASELINE_FILE_NAME)


def test_short_regression_open_positions() -> None:
    """期末未平倉部位比對：轉融券留倉與逐日計提的結果只存在於此"""

    _, positions, _ = run_all_scenarios()
    assert_matches_snapshot(positions, POSITION_FILE_NAME)


def test_short_regression_account_and_events() -> None:
    """帳戶終值與六個事件計數比對（event_counts 的 key 必須維持不變）"""

    _, _, summary = run_all_scenarios()
    assert_matches_snapshot(summary, SUMMARY_FILE_NAME)


@pytest.mark.parametrize(
    "scenario_name, expected_event",
    [
        ("margin_call_force_cover", "forced_cover_margin_call"),
        ("limit_up_locked_convert", "limit_up_cover_failed"),
        ("day_trade_on_force_cover_date", "forced_cover_day_trade"),
    ],
)
def test_scenarios_actually_trigger_events(
    scenario_name: str, expected_event: str
) -> None:
    """
    護欄有效性自檢：情境必須真的走到它宣稱的那條路徑

    否則快照可能只是「一組剛好都沒觸發任何規則的空跑」，改壞了也不會 fail。
    """

    summary: pd.DataFrame = pd.read_csv(SNAPSHOT_DIR / SUMMARY_FILE_NAME)
    row: pd.Series = summary[summary["Scenario"] == scenario_name].iloc[0]

    assert row[expected_event] >= 1, f"{scenario_name} 未觸發 {expected_event}"
