import argparse
import datetime
from pathlib import Path
from typing import List

import pandas as pd

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.strategies.stock.momentum_strategy_1 import MomentumStrategy1
from core.utils import TimeUtils

"""產生 LONG 策略的回歸 baseline：放空框架動程式碼前的第一步"""


SNAPSHOT_DIR: Path = Path(__file__).resolve().parent / "snapshots"
BASELINE_FILE_NAME: str = "momentum_strategy_1_baseline.csv"

# baseline 期間：2024 全年
#
# 原為 2024-01-01 ~ 06-30。2026-08-15 啟用股價還原時實測發現，該區間**對還原完全無感**：
# 台股除權息集中在 7~9 月（2024 年 67.2% 的事件落在下半年），上半年 736 筆事件中只有
# 4 筆跨越策略的 9% 門檻，其中 2 筆量能不足、2 筆撞上滿倉，最終逐筆相同。
# 若沿用半年區間，等於留下一條偵測不到還原行為的回歸線。
# 改為全年後，還原前後有 13 筆開倉差異（6 消失／7 新增），回歸線才真正保護這段邏輯。
BASELINE_START_DATE: datetime.date = datetime.date(2024, 1, 1)
BASELINE_END_DATE: datetime.date = datetime.date(2024, 12, 31)


def run_backtest_without_report(strategy: MomentumStrategy1) -> Backtester:
    """跑完回測主迴圈但不產生圖表報告（繪圖與回歸比對無關且耗時）"""

    backtester: Backtester = build_backtester(strategy)

    dates: List[datetime.date] = TimeUtils.generate_date_range(
        start_date=backtester.start_date, end_date=backtester.end_date
    )

    for date in dates:
        if not backtester.data_feed.is_market_open(date):
            continue

        backtester.run_day_backtest(date)

    backtester.account.update_account_status()
    return backtester


def generate_baseline() -> pd.DataFrame:
    """跑 MomentumStrategy1 並輸出交易明細，作為 LONG 路徑的回歸基準"""

    strategy: MomentumStrategy1 = MomentumStrategy1()
    strategy.start_date = BASELINE_START_DATE
    strategy.end_date = BASELINE_END_DATE

    backtester: Backtester = run_backtest_without_report(strategy)

    # 直接由 trade_records 組表，不經過 reporter：baseline 要釘住的是記帳結果，
    # 不應該因為報表層的欄位調整而失效
    rows: List[dict] = [
        {
            "Symbol": record.symbol,
            "Position Type": record.position_type.value,
            "Buy Date": record.buy_date,
            "Buy Price": record.buy_price,
            "Buy Volume": record.buy_volume,
            "Sell Date": record.sell_date,
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

    df: pd.DataFrame = pd.DataFrame(rows)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(SNAPSHOT_DIR / BASELINE_FILE_NAME, index=False)

    print(f"* Baseline saved: {SNAPSHOT_DIR / BASELINE_FILE_NAME}")
    print(f"* Trades: {len(df)}")
    print(f"* Balance: {backtester.account.balance}")
    print(f"* Realized PnL: {backtester.account.realized_pnl}")
    return df


if __name__ == "__main__":
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Generate LONG regression baseline"
    )
    parser.parse_args()

    generate_baseline()
