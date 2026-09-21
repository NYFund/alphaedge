from pathlib import Path
from typing import Dict, Optional

import pandas as pd
import pytest

from frontend.services.report_loader import (
    BacktestReport,
    build_equity_series,
    compute_daily_pnl,
    extract_starting_capital,
    load_backtest_report,
    read_daily_equity,
    read_direction_summary,
    read_event_report,
    read_trading_report,
    sort_by_exit_date,
    summarise_overview,
)

"""
前端只讀不算：指標與 reporter 落地的 CSV 逐值相同

fixture 是 `tests/fixtures/frontend_report/`——`Foreign-Sell-Short-Day-Trade`
那份報表的完整副本，**納入版控**。原本直接指向 `results/`，而該目錄被 `.gitignore`
忽略，於是 `skipif` 在 CI 必定成立：這 14 條防的是四個已經發生過的錯，卻從來沒有
在 CI 跑過一次，而 skip 在輸出裡只是一個 `s`，看起來是綠的。

**它是一份純 SHORT 的報表**，正好踩中舊版四個錯的每一個：

| 指標 | 舊版顯示 | 正確值 | 錯在哪 |
|------|:---:|:---:|--------|
| 平均 ROI | 82.1% | **0.82%** | `ROI` 欄已是百分比，又乘了一次 100 |
| 起始資金 | 1,011,269 | **1,000,000** | 取首列 `Cumulative Balance`，那是第一筆交易**之後**的餘額 |
| 資產曲線 | 依 `Sell Date` | 依 `Exit Date` | SHORT 的 `Sell Date` 是**開倉日** |
| MDD | 已實現口徑 | **−11.43%** | 沒讀 `daily_equity`，持倉期間的逆勢被抹平 |

**fixture 不可再改回 `results/`**：那等於把這一整組測試關掉。
"""


_FIXTURE_DIR: Path = Path(__file__).resolve().parent / "fixtures" / "frontend_report"

# fixture 的正確答案（由 reporter 的 CSV 直接讀出，不是另外算的）
EXPECTED_TRADES: int = 1096
EXPECTED_WIN_RATE: float = 59.76
EXPECTED_TOTAL_PNL: float = 3811419.0
EXPECTED_AVG_ROI: float = 0.82
EXPECTED_STARTING_CAPITAL: float = 1000000.0


@pytest.fixture
def report() -> BacktestReport:
    """載入 fixture 策略資料夾"""

    return load_backtest_report(_FIXTURE_DIR)


@pytest.fixture
def trading_df(report: BacktestReport) -> pd.DataFrame:
    assert report.csv_path is not None
    return read_trading_report(report.csv_path)


@pytest.fixture
def daily_equity_df(report: BacktestReport) -> pd.DataFrame:
    return read_daily_equity(report.daily_equity_path)


@pytest.fixture
def direction_summary_df(report: BacktestReport) -> pd.DataFrame:
    return read_direction_summary(report.direction_summary_path)


# === 三份 CSV 真的被讀進來===
def test_all_reporter_outputs_are_located(report: BacktestReport) -> None:
    """reporter 落地的四份 CSV 與五張圖，前端都要找得到"""

    assert report.csv_path is not None
    assert report.daily_equity_path is not None
    assert report.direction_summary_path is not None
    assert report.event_report_path is not None
    # 第 5 張圖（每日權益變化）舊版沒有讀
    assert report.chart_paths.get("每日權益變化") is not None
    assert all(path is not None for path in report.chart_paths.values())


def test_event_report_is_readable(report: BacktestReport) -> None:
    """事件計數讀得出來且有值——尾部風險被平均進總績效就看不見了"""

    events: pd.DataFrame = read_event_report(report.event_report_path)

    assert not events.empty
    assert list(events.columns) == ["Event", "Count"]
    counts: Dict[str, int] = dict(zip(events["Event"], events["Count"]))
    assert counts["limit_up_cover_failed"] == 10
    assert counts["forced_cover_suspended"] == 1


# === ROI 不再乘 100 ===
def test_overview_matches_direction_summary(
    trading_df: pd.DataFrame, direction_summary_df: pd.DataFrame
) -> None:
    """總覽四個數字與 `direction_summary.csv` 逐值相同"""

    overview: Dict[str, Optional[float]] = summarise_overview(
        trading_df, direction_summary_df
    )

    assert overview["trade_count"] == EXPECTED_TRADES
    assert overview["win_rate"] == pytest.approx(EXPECTED_WIN_RATE)
    assert overview["total_pnl"] == pytest.approx(EXPECTED_TOTAL_PNL)
    assert overview["avg_roi"] == pytest.approx(EXPECTED_AVG_ROI)


def test_overview_fallback_agrees_with_direction_summary(
    trading_df: pd.DataFrame, direction_summary_df: pd.DataFrame
) -> None:
    """
    沒有 `direction_summary.csv` 時的退路必須算出同一組數字

    兩條路徑給出不同答案，就是「同源」這件事沒有做到。
    """

    from_summary: Dict[str, Optional[float]] = summarise_overview(
        trading_df, direction_summary_df
    )
    from_trades: Dict[str, Optional[float]] = summarise_overview(
        trading_df, pd.DataFrame()
    )

    assert from_trades["trade_count"] == from_summary["trade_count"]
    assert from_trades["win_rate"] == pytest.approx(from_summary["win_rate"])
    assert from_trades["avg_roi"] == pytest.approx(from_summary["avg_roi"])
    assert from_trades["total_pnl"] == pytest.approx(from_summary["total_pnl"])


def test_average_roi_is_not_multiplied_again(trading_df: pd.DataFrame) -> None:
    """
    `ROI` 欄已經是百分比

    舊版的 `roi.mean() * 100` 會得到 82.1，與報表的 0.82 差 100 倍——
    這條測試就是釘住那個 100。
    """

    overview: Dict[str, Optional[float]] = summarise_overview(
        trading_df, pd.DataFrame()
    )

    assert overview["avg_roi"] == pytest.approx(EXPECTED_AVG_ROI)
    assert overview["avg_roi"] != pytest.approx(EXPECTED_AVG_ROI * 100)


# === 起始資金 ===
def test_starting_capital_excludes_first_trade_pnl(trading_df: pd.DataFrame) -> None:
    """
    起始資金 ＝ 首列 `Cumulative Balance` − 首列 `Realized PnL`

    直接取首列餘額會多算第一筆交易的損益（本 fixture 差 11,269）。
    """

    assert extract_starting_capital(trading_df) == pytest.approx(
        EXPECTED_STARTING_CAPITAL
    )
    assert float(trading_df["Cumulative Balance"].iloc[0]) != pytest.approx(
        EXPECTED_STARTING_CAPITAL
    )


def test_starting_capital_is_none_without_columns() -> None:
    """欄位缺漏時回 None，不要猜一個數字出來"""

    assert extract_starting_capital(pd.DataFrame()) is None


# === 權益序列與 MDD 走 daily_equity ===
def test_equity_series_starts_at_initial_capital(
    daily_equity_df: pd.DataFrame,
) -> None:
    """序列第一個節點是初始資金，不是第一個交易日結束後的權益"""

    equity: pd.Series = build_equity_series(daily_equity_df, EXPECTED_STARTING_CAPITAL)

    assert not equity.empty
    assert float(equity.iloc[0]) == pytest.approx(EXPECTED_STARTING_CAPITAL)
    assert equity.index.is_monotonic_increasing
    # 補了起點節點，長度比 CSV 多一列
    assert len(equity) == len(daily_equity_df) + 1


def test_daily_pnl_sums_to_total_gain(daily_equity_df: pd.DataFrame) -> None:
    """逐日損益加總 ＝ 期末權益 − 初始資金"""

    equity: pd.Series = build_equity_series(daily_equity_df, EXPECTED_STARTING_CAPITAL)
    daily_pnl: pd.Series = compute_daily_pnl(equity)

    assert float(daily_pnl.sum()) == pytest.approx(
        float(equity.iloc[-1]) - EXPECTED_STARTING_CAPITAL
    )


# === 一律 Exit Date 排序 ===
def test_sort_by_exit_date_differs_from_sell_date(trading_df: pd.DataFrame) -> None:
    """
    SHORT 的 `Sell Date` 是開倉日，兩種排序在本 fixture 確實不同

    這條測試若變成「兩者相同」，代表 fixture 換成了純 LONG 報表，
    此時它就抓不到這個 bug 了，要換一份含 SHORT 的報表。
    """

    by_exit: pd.DataFrame = sort_by_exit_date(trading_df)
    by_sell: pd.DataFrame = trading_df.copy()
    by_sell["Sell Date"] = pd.to_datetime(by_sell["Sell Date"], errors="coerce")
    by_sell = by_sell.sort_values("Sell Date")

    assert by_exit["Exit Date"].is_monotonic_increasing
    assert not by_exit.index.equals(by_sell.index)


def test_sort_by_exit_date_keeps_balance_monotonic(trading_df: pd.DataFrame) -> None:
    """
    依平倉日排序後，累積餘額就是報表裡原本的順序

    `Cumulative Balance` 是 reporter 依平倉順序累加出來的；排序對得上，
    這一欄就會與原表逐筆相同。
    """

    by_exit: pd.DataFrame = sort_by_exit_date(trading_df)

    assert by_exit["Cumulative Balance"].tolist() == (
        trading_df["Cumulative Balance"].tolist()
    )


def test_only_directories_with_a_report_are_listed(tmp_path: Path) -> None:
    """
    下拉選單只列有 trading report 的資料夾

    實盤輸出 `results/live/` 以前會被列成一支策略，選了之後停在
    「找不到 trading report CSV」；`logs/` 同理。
    """

    import shutil

    from frontend.services.report_loader import list_strategy_dirs

    shutil.copytree(_FIXTURE_DIR, tmp_path / "Foreign-Sell-Short-Day-Trade")
    (tmp_path / "live" / "Alpha").mkdir(parents=True)
    (tmp_path / "live" / "Alpha" / "2026-09-21_orders.csv").write_text("x\n")
    (tmp_path / "logs").mkdir()

    assert [path.name for path in list_strategy_dirs(tmp_path)] == [
        "Foreign-Sell-Short-Day-Trade"
    ]
