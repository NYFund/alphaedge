import datetime
from pathlib import Path
from typing import Callable, List

import pytest

from core.backtest.backtester import Backtester
from core.backtest.datafeed.tw.stock_datafeed import TwStockDataFeed
from core.backtest.factory import build_backtester
from core.config import BACKTEST_LOGS_DIR_PATH, BACKTEST_RESULT_DIR_PATH
from core.models import BaseOrder

"""
回測產物開關：實盤 parity 比對跑的單日回測不可寫報表與 backtest log

parity 比對每天盤後對每支策略跑一天回測。寫報表的話，研究者在 `results/<策略>/`
的多年期回測報表與圖每天都被蓋掉；backtest logger 是沒有 filter 的全域 sink，
掛上之後實盤行程的所有 log 也會寫進 `logs/backtest/<策略>.log`。
"""

DAY: datetime.date = datetime.date(2024, 1, 2)


@pytest.fixture
def no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """不載入資料庫、整段都休市：只留下「有沒有寫出產物」這件事可觀察"""

    monkeypatch.setattr(Backtester, "load_datasets", lambda self: None)
    monkeypatch.setattr(TwStockDataFeed, "is_market_open", lambda self, date: False)
    monkeypatch.setattr(TwStockDataFeed, "close", lambda self: None)


def artifact_paths(strategy_name: str) -> List[Path]:
    return [
        Path(BACKTEST_RESULT_DIR_PATH) / strategy_name,
        Path(BACKTEST_LOGS_DIR_PATH) / f"{strategy_name}.log",
    ]


def test_backtest_without_artifacts_writes_nothing(
    no_data: None, make_strategy: Callable[..., object]
) -> None:
    """`write_artifacts=False` 時結果資料夾與 backtest log 都不會出現"""

    strategy = make_strategy(
        start_date=DAY, end_date=DAY, strategy_name="ArtifactsOffStrategy"
    )

    backtester: Backtester = build_backtester(strategy, write_artifacts=False)
    backtester.run()

    assert [
        path for path in artifact_paths("ArtifactsOffStrategy") if path.exists()
    ] == []


def test_default_backtest_still_writes_its_result_dir(
    no_data: None, make_strategy: Callable[..., object]
) -> None:
    """
    預設行為不變：一般回測照樣建出結果資料夾

    這條是對照組——少了它，上一條可能只是因為這個環境本來就寫不出東西而通過。
    """

    strategy = make_strategy(
        start_date=DAY, end_date=DAY, strategy_name="ArtifactsOnStrategy"
    )

    build_backtester(strategy)

    assert (Path(BACKTEST_RESULT_DIR_PATH) / "ArtifactsOnStrategy").is_dir()


def test_parity_runner_does_not_write_backtest_artifacts(
    no_data: None, make_strategy: Callable[..., object]
) -> None:
    """
    parity 的單日回測 runner 不寫報表、不掛 backtest log

    這條真實的 runner 之前沒有任何測試，所以它每天蓋掉研究用報表一直沒人發現。
    """

    from core.live.factory import make_daily_backtest_runner

    base: type = type(make_strategy())

    class ParityOnlyStrategy(base):  # type: ignore[valid-type, misc]
        """只在本測試出現的名字：別的測試寫過同名資料夾就會造成順序相依"""

        def __init__(self) -> None:
            super().__init__()
            self.strategy_name = "ParityOnlyStrategy"

    runner = make_daily_backtest_runner([ParityOnlyStrategy()])

    orders: List[BaseOrder] = runner("ParityOnlyStrategy", DAY)

    assert orders == []
    assert [
        path for path in artifact_paths("ParityOnlyStrategy") if path.exists()
    ] == []
