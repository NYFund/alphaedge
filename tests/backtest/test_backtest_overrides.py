import datetime
import subprocess
from pathlib import Path
from typing import List

import pytest

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.backtest.overrides import BacktestOverrides, InvalidBacktestOverridesError
from core.config import TW_STOCK_DB_PATH
from strategies.futures.momentum_futures_strategy import MomentumFuturesStrategy
from strategies.stock.volume_breakout_momentum_strategy import (
    VolumeBreakoutMomentumStrategy,
)
from tests.entry_sandbox import run_isolated

"""
回測區間與初始資金的覆寫（`BacktestOverrides`、`python -m apps.backtest --start/--end/--capital`）

覆寫一定要在組裝任何元件之前寫回策略：帳戶讀資金、資料源與引擎讀起訖日，
晚一步就會有元件拿到舊值。這裡除了驗證規則，也直接檢查組裝出來的元件拿到的是新值。
"""


# === 覆寫規則 ===
def test_partial_override_keeps_the_other_defaults() -> None:
    """只給起日時，迄日與資金沿用策略預設"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    default_end: datetime.date = strategy.end_date
    default_capital: float = strategy.init_capital

    BacktestOverrides(start=datetime.date(2024, 1, 2)).apply_to(strategy)

    assert strategy.start_date == datetime.date(2024, 1, 2)
    assert strategy.end_date == default_end
    assert strategy.init_capital == default_capital


def test_start_after_the_default_end_is_refused() -> None:
    """
    只給起日、但它晚於策略預設的迄日：要拿**覆寫後的組合**驗證

    逐欄驗證（只看 start 自己合不合法）會放過這種組合。
    """

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    later_than_end: datetime.date = strategy.end_date + datetime.timedelta(days=1)

    with pytest.raises(InvalidBacktestOverridesError, match="晚於迄日"):
        BacktestOverrides(start=later_than_end).apply_to(strategy)


def test_refused_override_leaves_the_strategy_untouched() -> None:
    """驗證不過時策略維持原狀，不可以只改了一半"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    before: tuple = (strategy.start_date, strategy.end_date, strategy.init_capital)

    with pytest.raises(InvalidBacktestOverridesError):
        BacktestOverrides(
            start=datetime.date(2025, 1, 1),
            end=datetime.date(2024, 1, 1),
            capital=1.0,
        ).apply_to(strategy)

    assert (strategy.start_date, strategy.end_date, strategy.init_capital) == before


@pytest.mark.parametrize(
    ("strategy_cls", "too_early"),
    [
        (VolumeBreakoutMomentumStrategy, datetime.date(2012, 12, 31)),
        (MomentumFuturesStrategy, datetime.date(2014, 12, 31)),
    ],
    ids=["stock-before-2013", "futures-before-2015"],
)
def test_start_before_the_data_start_is_refused(
    strategy_cls: type, too_early: datetime.date
) -> None:
    """早於歷史資料起點的區間查不到任何行情，回測會整段空跑而不報錯"""

    with pytest.raises(InvalidBacktestOverridesError, match="歷史資料起點"):
        BacktestOverrides(start=too_early).apply_to(strategy_cls())


@pytest.mark.parametrize("capital", [0.0, -1.0])
def test_non_positive_capital_is_refused(capital: float) -> None:
    """資金為 0 或負數時帳戶一開始就無法下單"""

    with pytest.raises(InvalidBacktestOverridesError, match="初始資金"):
        BacktestOverrides(capital=capital).apply_to(VolumeBreakoutMomentumStrategy())


# === 組裝出來的元件拿到新值 ===
@pytest.fixture
def no_data_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `Backtester.__init__` 最後會 `setup()` 讀資料庫；這裡只驗組裝時拿到的值，不需要資料

    **不換掉的話，沒有資料庫的環境會被 SQLite 順手建出一個空的 `tw_stock.db`**，
    之後「資料庫存在才跑」的測試就會對著空檔跑而失敗。
    """

    monkeypatch.setattr(Backtester, "setup", lambda self: None)


@pytest.mark.parametrize(
    "strategy_cls",
    [VolumeBreakoutMomentumStrategy, MomentumFuturesStrategy],
    ids=["stock", "futures"],
)
def test_components_are_built_with_the_overridden_values(
    strategy_cls: type, no_data_setup: None
) -> None:
    """帳戶資金與引擎起訖日都是覆寫值：證明覆寫發生在組裝之前"""

    overrides: BacktestOverrides = BacktestOverrides(
        start=datetime.date(2024, 3, 1),
        end=datetime.date(2024, 3, 29),
        capital=123456.0,
    )

    backtester: Backtester = build_backtester(
        strategy_cls(), write_artifacts=False, overrides=overrides
    )

    assert backtester.account.init_capital == 123456.0
    assert backtester.start_date == datetime.date(2024, 3, 1)
    assert backtester.end_date == datetime.date(2024, 3, 29)


def test_no_overrides_keeps_the_strategy_defaults(no_data_setup: None) -> None:
    """不帶覆寫時與原本完全相同（回歸雙線的前提）"""

    strategy: VolumeBreakoutMomentumStrategy = VolumeBreakoutMomentumStrategy()
    defaults: tuple = (strategy.start_date, strategy.end_date, strategy.init_capital)

    backtester: Backtester = build_backtester(strategy, write_artifacts=False)

    assert (
        backtester.start_date,
        backtester.end_date,
        backtester.account.init_capital,
    ) == defaults


# === 入口（子行程） ===
@pytest.mark.parametrize(
    "flags",
    [
        ["--start", "2025-01-01", "--end", "2024-01-01"],
        ["--start", "2012-01-01"],
        ["--capital", "0"],
        ["--start", "2025/01/01"],
    ],
    ids=["start-after-end", "before-data-start", "zero-capital", "bad-format"],
)
def test_invalid_overrides_exit_with_usage_error(flags: List[str]) -> None:
    """不合法的覆寫是用法錯誤（退出碼 2），在跑任何回測之前就結束"""

    result: subprocess.CompletedProcess = run_isolated(
        ["-m", "apps.backtest", "--strategy", "VolumeBreakoutMomentumStrategy", *flags]
    )

    assert result.returncode == 2


# === 真實資料 ===
@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(), reason="需要 tw_stock.db 才能實跑回測"
)
def test_trades_fall_inside_the_overridden_range() -> None:
    """帶起訖日時，所有交易的日期都落在區間內，而且區間內確實有交易"""

    start: datetime.date = datetime.date(2024, 3, 1)
    end: datetime.date = datetime.date(2024, 4, 30)
    backtester: Backtester = build_backtester(
        VolumeBreakoutMomentumStrategy(),
        write_artifacts=False,
        overrides=BacktestOverrides(start=start, end=end),
    )

    backtester.run()
    records: list = backtester.account.trade_records

    assert records, "區間內沒有任何交易，這條斷言會變成恆真"
    for record in records:
        assert start <= record.buy_date <= end
        if record.is_closed:
            assert start <= record.sell_date <= end
