import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import pandas as pd
import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.report.plotting import EquityChartRenderer
from core.backtest.report.reporter import StockBacktestReporter
from core.dao.base import BaseDAO
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.models import StockAccount, StockOrder, StockTradeRecord
from core.utils import Action, PositionType, ShortMethod

"""每日權益、多空分開統計與事件報表的測試"""


DAY_1: datetime.date = datetime.date(2024, 1, 2)


@pytest.fixture
def make_backtester(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Backtester]:
    """建立不載入資料庫的 Backtester"""

    def _make_backtester(strategy) -> Backtester:
        monkeypatch.setattr(Backtester, "setup", lambda self: None)
        return build_backtester(strategy)

    return _make_backtester


def test_snapshot_daily_equity_includes_unrealized(
    make_strategy, make_backtester, make_quote
) -> None:
    """留倉放空的帳面虧損必須反映在每日權益，不能等到平倉才出現"""

    strategy = make_strategy(
        position_type=PositionType.SHORT,
        enable_intraday=False,
        short_method=ShortMethod.MARGIN,
        open_script={
            DAY_1: [
                StockOrder(
                    stock_id="2330",
                    date=DAY_1,
                    action=Action.SELL,
                    position_type=PositionType.SHORT,
                    price=100.0,
                    volume=1,
                )
            ]
        },
    )
    backtester: Backtester = make_backtester(strategy)

    backtester.execute_bar(
        DAY_1, [make_quote(date=DAY_1, cur_price=100.0, high=101.0, low=99.0)]
    )

    # 開倉當日：現金 1000000 − 90422 + 部位（保證金 90000 + 未實現 0）
    assert backtester.daily_equity[0]["Equity"] == 999578.0

    # 次日股價上漲 5 元，未實現虧損 5000 應立刻反映在權益上
    day_2: datetime.date = datetime.date(2024, 1, 3)
    backtester.execute_bar(
        day_2, [make_quote(date=day_2, cur_price=105.0, high=106.0, low=104.0)]
    )

    assert backtester.account.get_positions()[0].unrealized_pnl == -5000.0
    assert backtester.daily_equity[1]["Equity"] == 994578.0
    assert backtester.account.realized_pnl == 0.0  # 尚未平倉，已實現損益仍為 0


def test_trading_report_columns_and_symbol(
    make_strategy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    報表的欄位名與識別欄位取值

    識別欄名與領域模型一致（`record.symbol` → `Symbol`）。期貨那份維持
    `Contract ID`：它是 `{商品}{到期月}`，與「一檔股票」不是同一種東西，
    而且前端以它判斷報表型別（見 `frontend/services/futures_metrics.py`）。

    **回歸雙線完全不經過 reporter**（它們自行從 `trade_records` 組表），
    所以報表的欄位名與取值只能靠本測試把關——改錯了回歸不會變紅。
    """

    monkeypatch.setattr(StockBacktestReporter, "setup", lambda self: None)

    strategy = make_strategy(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
    )
    account: StockAccount = StockAccount(1000000.0)
    strategy.setup_account(account)

    account.trade_records.append(
        StockTradeRecord(
            id=1,
            stock_id="2330",
            is_closed=True,
            position_type=PositionType.LONG,
            buy_date=DAY_1,
            buy_price=100.0,
            buy_volume=1,
            sell_date=datetime.date(2024, 1, 5),
            sell_price=105.0,
            sell_volume=1,
            realized_pnl=4548.0,
            roi=4.53,
        )
    )

    reporter: StockBacktestReporter = StockBacktestReporter(strategy, tmp_path)
    reporter.account = account
    report: pd.DataFrame = reporter.generate_trading_report()

    # 欄位名維持台股語意，且順序不變（baseline 逐欄比對依賴此順序）
    assert list(report.columns)[:3] == ["Symbol", "Position Type", "Entry Date"]
    assert "Stock ID" not in report.columns

    # 取值來自 model 的 symbol，不是空字串
    assert report.loc[0, "Symbol"] == "2330"
    assert report.loc[0, "Symbol"] == account.trade_records[0].symbol


def test_direction_summary_and_event_report(
    make_strategy, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多空分開統計與事件計數需正確輸出"""

    monkeypatch.setattr(StockBacktestReporter, "setup", lambda self: None)

    strategy = make_strategy(
        start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
    )
    account: StockAccount = StockAccount(1000000.0)
    strategy.setup_account(account)

    account.trade_records.append(
        StockTradeRecord(
            id=1,
            stock_id="2330",
            is_closed=True,
            position_type=PositionType.SHORT,
            short_method=ShortMethod.MARGIN,
            sell_date=DAY_1,
            sell_price=100.0,
            buy_date=datetime.date(2024, 1, 12),
            buy_price=95.0,
            commission=82.0,
            tax=300.0,
            borrow_fee=80.0,
            interest=10.0,
            margin=90000.0,
            holding_days=10,
            realized_pnl=4548.0,
            roi=4.53,
            roi_on_capital=5.03,
        )
    )
    account.trade_records.append(
        StockTradeRecord(
            id=2,
            stock_id="2317",
            is_closed=True,
            position_type=PositionType.LONG,
            buy_date=DAY_1,
            buy_price=50.0,
            sell_date=datetime.date(2024, 1, 5),
            sell_price=48.0,
            commission=40.0,
            tax=144.0,
            realized_pnl=-2184.0,
            roi=-4.37,
        )
    )

    reporter: StockBacktestReporter = StockBacktestReporter(strategy, tmp_path)
    reporter.account = account
    reporter.trading_report = reporter.generate_trading_report()

    summary: pd.DataFrame = reporter.generate_direction_summary()
    short_row: pd.Series = summary[summary["Position Type"] == "SHORT"].iloc[0]
    long_row: pd.Series = summary[summary["Position Type"] == "LONG"].iloc[0]

    assert short_row["Trades"] == 1
    assert short_row["Win Rate (%)"] == 100.0
    assert short_row["Total Borrow Fee"] == 80.0
    assert short_row["Total Interest"] == 10.0
    assert short_row["Avg Holding Days"] == 10.0
    assert long_row["Total PnL"] == -2184.0
    assert long_row["Total Borrow Fee"] == 0.0

    events: Dict[str, int] = {"forced_cover_day_trade": 3, "limit_up_cover_failed": 1}
    event_df: pd.DataFrame = reporter.generate_event_report(events)

    assert set(event_df["Event"]) == set(events.keys())
    assert event_df[event_df["Event"] == "limit_up_cover_failed"]["Count"].iloc[0] == 1


# === 分割調整：已由 corporate_action 經還原係數統一處理（還原價 S3）===
def test_adjustment_factor_covers_splits_and_reductions(
    dao_factory: Callable[..., BaseDAO],
) -> None:
    """
    分割與減資都要進累乘係數，不再靠一份只認得 0050 的過渡表

    `core/api/tw/stock_split.py` 於 2026-09-13 刪除。在那之前，還原係數只認
    除權息（`stock_dividend` 不含分割），所以 reporter 得自己
    再套一次分割調整才拿得到正確的 benchmark；`corporate_action` 表上線後
    改由 `get_adjusted_close_series()` 一次處理完。
    """

    from core.api.tw.stock_dividend_api import StockDividendAPI

    dividend_dao: BaseDAO = dao_factory(
        StockDividendDAO,
        records=[{"date": "2025-03-01", "stock_id": "0050", "還原係數": 0.98}],
    )
    dao_factory(
        CorporateActionDAO,
        records=[
            {
                "date": "2025-06-18",
                "stock_id": "0050",
                "調整倍率": 0.25,  # 一拆四：價格變四分之一
            }
        ],
    )

    api: StockDividendAPI = StockDividendAPI(conn=dividend_dao.conn)

    before: float = api.get_cumulative_factor("0050", datetime.date(2025, 6, 10))
    after: float = api.get_cumulative_factor("0050", datetime.date(2025, 6, 18))

    # 分割後的係數應為分割前的 4 倍（1 / 0.25），才能把 −75% 的假跌幅補回來
    assert after / before == pytest.approx(4.0)


def test_reporter_no_longer_double_adjusts() -> None:
    """
    reporter 不可再對還原價套一次分割調整

    **重複調整實測會讓 0050 的分割日由 1.82% 變成 303%**。這條釘住
    `get_adjusted_price()` 已退化為原樣回傳——它保留只是為了讓兩處呼叫端
    不必各自改。
    """

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    raw: pd.Series = pd.Series(
        [188.65, 47.57],
        index=[datetime.date(2025, 6, 10), datetime.date(2025, 6, 18)],
    )

    assert reporter.get_adjusted_price(raw, "0050").equals(raw)


def test_transitional_split_table_is_gone() -> None:
    """
    過渡表已刪除，不得有人再 import 它

    留著會出現兩份分割來源，而抄漏一次分割的代價是整段序列從那天起錯 N 倍。
    """

    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.api.tw.stock_split")


# === reporter 可維護性===
def test_benchmark_uses_adjusted_close() -> None:
    """
    benchmark 取的是**還原**收盤價，不是原始收盤價

    0050 年年配息，用原始價當基準等於讓基準每年少賺一次配息，策略看起來
    永遠贏得比實際多。
    """

    import datetime as dt

    calls: List[str] = []

    class _Price:
        def get_adjusted_close_series(self, stock_id, start_date, end_date):
            calls.append("adjusted")
            return pd.Series(
                [100.0, 110.0],
                index=[dt.date(2024, 1, 2), dt.date(2024, 1, 3)],
            )

        def get_stock_price(self, *args, **kwargs):  # pragma: no cover - 不該被呼叫
            calls.append("raw")
            return pd.DataFrame()

        def close(self) -> None:
            calls.append("closed")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    reporter.price = _Price()
    reporter.benchmark = "0050"
    reporter.start_date = dt.date(2024, 1, 1)
    reporter.end_date = dt.date(2024, 1, 31)
    reporter.setup()

    assert calls == ["adjusted"]
    assert list(reporter.benchmark_price) == [100.0, 110.0]


def test_reporter_close_releases_only_its_own_connection() -> None:
    """
    共用連線不歸 reporter 關

    `StockPriceAPI` 以 `owns_conn` 區分；reporter 只是把 `close()` 轉發過去，
    自己不判斷。判斷寫兩份就會有一份漏掉。
    """

    closed: List[str] = []

    class _Price:
        def close(self) -> None:
            closed.append("price")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    reporter.price = _Price()
    reporter.close()

    assert closed == ["price"]

    # 期貨報表不建 StockPriceAPI（`setup()` 把 price 設成 None），不得炸
    reporter.price = None
    reporter.close()


def test_show_figures_defaults_to_off(monkeypatch) -> None:
    """
    預設不開瀏覽器

    reporter 有五張圖，舊版 `set_figure_config(show=True)` 寫死，每跑一次回測
    就彈出 5 個分頁；批次掃參數時一次開幾十個，無頭環境更是直接失敗。
    """

    from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures

    monkeypatch.delenv(SHOW_FIGURES_ENV_VAR, raising=False)
    assert resolve_show_figures() is False

    monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, "1")
    assert resolve_show_figures() is True


def test_show_figures_only_accepts_explicit_truthy(monkeypatch) -> None:
    """
    `ALPHAEDGE_SHOW_FIGURES=0` 是關，不是「有設就開」

    習慣寫 `VAR=0` 關功能的人踩到反效果，會是最難查的那種問題。
    """

    from core.config import SHOW_FIGURES_ENV_VAR, resolve_show_figures

    for value in ("0", "false", "no", "off", ""):
        monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, value)
        assert resolve_show_figures() is False, value

    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(SHOW_FIGURES_ENV_VAR, value)
        assert resolve_show_figures() is True, value


def test_set_figure_config_does_not_open_browser_by_default() -> None:
    """`show` 不指定時跟隨 reporter 的設定，不是寫死 True"""

    import plotly.graph_objects as go

    opened: List[str] = []

    class _Figure(go.Figure):
        def show(self, *args, **kwargs) -> None:
            opened.append("shown")

    reporter: StockBacktestReporter = StockBacktestReporter.__new__(
        StockBacktestReporter
    )
    # 繞過 `__init__` 就沒有渲染器；繪圖已搬到 `EquityChartRenderer`，此處自行補上
    reporter.renderer = EquityChartRenderer(reporter)
    reporter.show = False
    reporter.set_figure_config(_Figure(), title="t")
    assert opened == []

    reporter.show = True
    reporter.set_figure_config(_Figure(), title="t")
    assert opened == ["shown"]


# === 整體績效指標 ===
def make_metrics_reporter(
    strategy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pnls: List[float],
    daily_equity: Optional[List[Dict[str, object]]] = None,
) -> StockBacktestReporter:
    """組出帶指定損益與（可選）逐日權益的 reporter"""

    monkeypatch.setattr(StockBacktestReporter, "setup", lambda self: None)

    account: StockAccount = StockAccount(1000000.0)
    strategy.setup_account(account)
    for index, pnl in enumerate(pnls, start=1):
        account.trade_records.append(
            StockTradeRecord(
                id=index,
                stock_id="2330",
                is_closed=True,
                position_type=PositionType.LONG,
                buy_date=DAY_1,
                buy_price=100.0,
                buy_volume=1,
                sell_date=datetime.date(2024, 1, index + 1),
                sell_price=100.0 + pnl / 1000,
                sell_volume=1,
                realized_pnl=pnl,
                roi=round(pnl / 100000 * 100, 2),
            )
        )

    reporter: StockBacktestReporter = StockBacktestReporter(strategy, tmp_path)
    reporter.account = account
    reporter.benchmark_price = pd.Series(dtype=float)
    reporter.daily_equity = daily_equity or []
    reporter.trading_report = reporter.generate_trading_report()
    return reporter


def metrics_map(df: pd.DataFrame) -> Dict[str, object]:
    """長表轉 `{Metric: Value}`"""

    return dict(zip(df["Metric"], df["Value"]))


def test_metrics_summary_is_a_long_table(make_strategy, tmp_path, monkeypatch) -> None:
    """
    格式是 `Metric`／`Value`／`Note` 長表

    **不是寬表**：新增一個指標就要改欄位結構的話，每加一項都會讓既有的
    下游讀取壞掉一次。
    """

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0, 800.0],
    )

    df: pd.DataFrame = reporter.generate_metrics_summary()

    assert list(df.columns) == ["Metric", "Value", "Note"]
    assert (
        tmp_path / f"{reporter.strategy.strategy_name}_metrics_summary.csv"
    ).exists()


def test_metrics_summary_trade_statistics_match_hand_calculation(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """交易統計逐項與手算一致"""

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0, 800.0],
    )

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Trades"] == 3
    assert values["Win Count"] == 2
    assert values["Loss Count"] == 1
    assert values["Win Rate (%)"] == pytest.approx(66.67, abs=0.01)
    assert values["Win/Loss Ratio"] == pytest.approx(2.0)
    assert values["Profit Factor"] == pytest.approx(1800 / 500)
    assert values["Total PnL"] == pytest.approx(1300.0)


def test_overall_win_rate_matches_the_direction_summary(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """
    整體勝率與 `direction_summary` 依 `Trades` 加權合併的結果必須相同

    同一個數字在兩張報表上不一致，讀的人無從判斷哪個對。
    """

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0, 800.0, -200.0],
    )

    values = metrics_map(reporter.generate_metrics_summary())
    summary: pd.DataFrame = reporter.generate_direction_summary()

    weighted: float = (summary["Win Rate (%)"] * summary["Trades"]).sum() / summary[
        "Trades"
    ].sum()

    assert values["Win Rate (%)"] == pytest.approx(weighted, abs=0.01)


def test_daily_metrics_are_blank_without_daily_equity(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """
    `Realized only` 口徑下，波動度與 Sharpe／Sortino 一律留空

    那個口徑只在平倉那天才有節點，拿它的逐筆報酬乘 √252 等於宣稱一年有 252 筆
    交易——留空比給一個看起來合理的錯數字好。**MDD 仍然輸出**（它不需要等距樣本）。
    """

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0, 800.0],
    )

    df: pd.DataFrame = reporter.generate_metrics_summary()
    values = metrics_map(df)
    notes = dict(zip(df["Metric"], df["Note"]))

    assert values["Equity Basis"] == StockBacktestReporter.EQUITY_BASIS_REALIZED_ONLY
    for metric in ("Annualized Volatility (%)", "Sharpe Ratio", "Sortino Ratio"):
        assert values[metric] is None
        assert "252" in notes[metric]

    assert values["Max Drawdown (%)"] is not None


def test_daily_metrics_are_computed_with_daily_equity(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """有逐日權益時三項風險指標照算（防止留空的條件寫得太寬）"""

    equity_rows: List[Dict[str, object]] = [
        {"Date": datetime.date(2024, 1, day), "Equity": value}
        for day, value in enumerate(
            [1000000.0, 1010000.0, 1005000.0, 1020000.0, 1015000.0], start=2
        )
    ]

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0],
        daily_equity=equity_rows,
    )

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Equity Basis"] == StockBacktestReporter.EQUITY_BASIS_MARK_TO_MARKET
    assert values["Annualized Volatility (%)"] is not None
    assert values["Sharpe Ratio"] is not None


def test_information_ratio_needs_an_aligned_benchmark(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """
    IR 以**日期交集**對齊後才逐日相減

    長度湊得起來不代表日期對得起來；自作主張對齊只會讓錯位的比較看起來很正常。
    """

    equity_rows: List[Dict[str, object]] = [
        {"Date": datetime.date(2024, 1, day), "Equity": value}
        for day, value in enumerate(
            [1000000.0, 1010000.0, 1005000.0, 1020000.0], start=2
        )
    ]

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0],
        daily_equity=equity_rows,
    )
    # 基準比策略多一天（1/6），交集之後兩邊長度才會一致。
    # 走勢刻意與策略不同——完全同步的話追蹤誤差為 0，IR 依定義就是 None
    reporter.benchmark_price = pd.Series(
        [100.0, 100.3, 101.2, 100.8, 103.0],
        index=[datetime.date(2024, 1, day) for day in range(2, 7)],
    )

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Benchmark"] == reporter.benchmark
    assert values["Information Ratio"] is not None


def test_information_ratio_is_blank_without_daily_equity(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """`Realized only` 口徑與基準日報酬不同頻，IR 留空並說明原因"""

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0],
    )

    df: pd.DataFrame = reporter.generate_metrics_summary()
    notes = dict(zip(df["Metric"], df["Note"]))

    assert metrics_map(df)["Information Ratio"] is None
    assert "不同頻" in notes["Information Ratio"]


def test_metrics_summary_is_empty_without_closed_trades(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """沒有已平倉交易時回空表，不是一堆 0"""

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[],
    )

    df: pd.DataFrame = reporter.generate_metrics_summary()

    assert df.empty
    assert list(df.columns) == ["Metric", "Value", "Note"]


# === 滑價成本統計 ===
def test_slippage_cost_is_reported(make_strategy, tmp_path, monkeypatch) -> None:
    """
    滑價吃掉的價差要看得見

    報表原本只知道「有沒有開滑價」，不知道它總共吃掉多少——一支策略的績效若有
    三成被滑價吃掉，那是該被看見的事實。
    """

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[1000.0, -500.0],
    )
    reporter.account.total_slippage_cost = 125.0

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Slippage Cost"] == pytest.approx(125.0)
    # 500 元損益、125 元滑價 → 25%
    assert values["Slippage Cost / |Total PnL| (%)"] == pytest.approx(25.0)


def test_slippage_share_uses_the_absolute_pnl(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """
    分母取絕對值——虧損的策略同樣要看得到滑價佔比

    直接除以負的損益會讓「滑價佔比」帶一個看不懂的負號。
    """

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[-1000.0],
    )
    reporter.account.total_slippage_cost = 200.0

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Slippage Cost / |Total PnL| (%)"] == pytest.approx(20.0)


def test_slippage_share_is_blank_when_pnl_is_zero(
    make_strategy, tmp_path, monkeypatch
) -> None:
    """損益為 0 時比例沒有定義，留空而不是除以零"""

    reporter = make_metrics_reporter(
        make_strategy(
            start_date=datetime.date(2024, 1, 1), end_date=datetime.date(2024, 3, 31)
        ),
        tmp_path,
        monkeypatch,
        pnls=[500.0, -500.0],
    )
    reporter.account.total_slippage_cost = 50.0

    values = metrics_map(reporter.generate_metrics_summary())

    assert values["Slippage Cost"] == pytest.approx(50.0)
    assert values["Slippage Cost / |Total PnL| (%)"] is None
