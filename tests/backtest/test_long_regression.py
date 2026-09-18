from pathlib import Path

import pandas as pd
import pytest

from core.config import TW_STOCK_DB_PATH
from tests.backtest.make_baseline import (
    BASELINE_FILE_NAME,
    SNAPSHOT_DIR,
    run_backtest_without_report,
)

"""LONG 路徑回歸測試：放空框架的任何改動都不得影響既有做多策略的結果"""


pytestmark = pytest.mark.slow


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(), reason="需要 tw_stock.db 才能重跑 LONG 回歸"
)
def test_long_regression_snapshot() -> None:
    """重跑 MomentumStrategy1 並與 baseline 逐筆比對"""

    from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
    from tests.backtest.make_baseline import BASELINE_END_DATE, BASELINE_START_DATE

    baseline_path: Path = SNAPSHOT_DIR / BASELINE_FILE_NAME
    assert baseline_path.exists(), "baseline 不存在，請先執行 make_baseline.py"

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.start_date = BASELINE_START_DATE
    strategy.end_date = BASELINE_END_DATE

    backtester = run_backtest_without_report(strategy)

    actual: pd.DataFrame = pd.DataFrame(
        [
            {
                "Symbol": record.symbol,
                "Position Type": record.position_type.value,
                "Buy Date": str(record.buy_date),
                "Buy Price": record.buy_price,
                "Buy Volume": record.buy_volume,
                "Sell Date": str(record.sell_date),
                "Sell Price": record.sell_price,
                "Sell Volume": record.sell_volume,
                "Commission": record.commission,
                "Tax": record.tax,
                "Transaction Cost": record.transaction_cost,
                "Realized PnL": record.realized_pnl,
                "ROI": record.roi,
            }
            for record in backtester.account.trade_records
            if record.is_closed
        ]
    )

    expected: pd.DataFrame = pd.read_csv(baseline_path, dtype={"Symbol": str})
    expected["Buy Date"] = expected["Buy Date"].astype(str)
    expected["Sell Date"] = expected["Sell Date"].astype(str)

    assert len(actual) == len(expected), "交易筆數與 baseline 不同"
    pd.testing.assert_frame_equal(
        actual.reset_index(drop=True),
        expected[actual.columns].reset_index(drop=True),
        check_dtype=False,
    )
