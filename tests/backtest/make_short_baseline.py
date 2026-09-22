import argparse
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.backtest.backtester import Backtester
from core.backtest.factory import build_backtester
from core.models import StockOrder, StockQuote
from core.models.cost_config import ShortConstraint
from core.utils import Action, PositionType, Scale, ShortMethod
from tests.backtest.conftest import ScriptedDataFeed, ScriptedStrategy

"""
產生 SHORT 路徑的回歸 baseline：多市場抽象重構動程式碼前的第一步

LONG 已有 915 筆逐筆 baseline，SHORT 卻只有單元／整合測試，缺少「整條路徑的完整帳」。
Phase 2 要把放空記帳從 Backtester 搬進 SettlementModel，若某個攤提比例走鐘，
現有測試不保證抓得到，因此在此建立第二條回歸線。

本檔案完全不連資料庫：報價與訊號都由腳本給定，跑一次僅需秒級。
"""


SNAPSHOT_DIR: Path = Path(__file__).resolve().parent / "snapshots"
BASELINE_FILE_NAME: str = "short_regression_baseline.csv"  # 逐筆交易紀錄
POSITION_FILE_NAME: str = "short_regression_positions.csv"  # 期末未平倉部位
SUMMARY_FILE_NAME: str = "short_regression_summary.csv"  # 帳戶終值與事件計數

# 情境基準日：固定為 2024-01-02，所有日期以此為原點推算（融券利息依曆日數計算）
BASE_DATE: datetime.date = datetime.date(2024, 1, 2)

# 所有情境共用的標的與本金
STOCK_ID: str = "2330"
INIT_CAPITAL: float = 1000000.0


def build_scripted_backtester(strategy: ScriptedStrategy) -> Backtester:
    """
    以 factory 組出台股 model 組合，但跳過 setup()

    setup() 會建立結果目錄、log 檔與五個資料 API 的連線，本回歸線完全以腳本
    驅動、不連 DB，因此在建構期間暫時停用它。走 factory 而非自行 new，
    是為了讓快照驗證的是「實際會被 run.py 使用的那組 model 組合」。
    """

    original_setup = Backtester.setup
    Backtester.setup = lambda self: None
    try:
        return build_backtester(strategy)
    finally:
        Backtester.setup = original_setup


class ShortScenario:
    """一組確定性的放空情境：策略腳本 ＋ 逐日報價"""

    def __init__(
        self,
        name: str,
        verifies: str,
        strategy: ScriptedStrategy,
        bars: List[Tuple[datetime.date, List[StockQuote]]],
        force_cover_script: Optional[Dict[datetime.date, Set[str]]] = None,
        cash_dividend_script: Optional[Dict[datetime.date, Dict[str, float]]] = None,
    ) -> None:
        self.name: str = name  # 情境名稱（快照的分組鍵）
        self.verifies: str = verifies  # 這個情境驗的是什麼
        self.strategy: ScriptedStrategy = strategy
        self.bars: List[Tuple[datetime.date, List[StockQuote]]] = bars  # 逐日報價

        # 停券日與除息股利由 DataFeed 每根 bar 推給 SettlementModel，
        # 故腳本掛在資料源那一層而非策略上
        self.force_cover_script: Dict[datetime.date, Set[str]] = (
            force_cover_script or {}
        )
        self.cash_dividend_script: Dict[datetime.date, Dict[str, float]] = (
            cash_dividend_script or {}
        )


def day(offset: int) -> datetime.date:
    """取得距基準日 offset 個曆日的日期"""

    return BASE_DATE + datetime.timedelta(days=offset)


def make_quote(
    date: datetime.date,
    cur_price: float,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
) -> StockQuote:
    """建立日 K 報價；未指定的 OHLC 一律沿用 cur_price"""

    return StockQuote(
        stock_id=STOCK_ID,
        scale=Scale.DAY,
        date=date,
        cur_price=cur_price,
        volume=1000,
        open=cur_price,
        high=high if high is not None else cur_price,
        low=low if low is not None else cur_price,
        close=close if close is not None else cur_price,
    )


def make_short_order(
    date: datetime.date,
    action: Action,
    price: float,
    volume: int,
) -> StockOrder:
    """建立放空訂單：SELL 為開倉、BUY 為回補"""

    return StockOrder(
        stock_id=STOCK_ID,
        date=date,
        action=action,
        position_type=PositionType.SHORT,
        price=price,
        volume=volume,
    )


def make_short_strategy(**overrides: Any) -> ScriptedStrategy:
    """建立放空用的 ScriptedStrategy；預設為留倉融券，可逐欄位覆寫"""

    strategy: ScriptedStrategy = ScriptedStrategy(
        open_script=overrides.pop("open_script", None),
        close_script=overrides.pop("close_script", None),
    )

    strategy.init_capital = INIT_CAPITAL
    strategy.position_type = PositionType.SHORT
    strategy.enable_intraday = False
    strategy.short_method = ShortMethod.MARGIN

    for key, value in overrides.items():
        setattr(strategy, key, value)

    return strategy


def build_scenarios() -> List[ShortScenario]:
    """
    - Description:
        建立涵蓋放空記帳各條路徑的六組情境

        每組都刻意只動一個變因，任一情境的快照有變即可直接指向出問題的掛點。
    - Return:
        - List[ShortScenario]
            所有回歸情境
    """

    scenarios: List[ShortScenario] = []

    # === 1. 當沖放空同日回補：DAY_TRADE 稅率減半、OPEN_THEN_CLOSE ===
    scenarios.append(
        ShortScenario(
            name="day_trade_same_day_cover",
            verifies="DAY_TRADE 稅率減半、OPEN_THEN_CLOSE 同 bar 開平倉",
            strategy=make_short_strategy(
                enable_intraday=True,
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 2)]},
                close_script={day(0): [make_short_order(day(0), Action.BUY, 95.0, 2)]},
            ),
            bars=[(day(0), [make_quote(day(0), 97.0, high=101.0, low=94.0)])],
        )
    )

    # === 2. 融券留倉 10 天後回補：保證金佔用、融券利息於平倉一次算 ===
    margin_swing_bars: List[Tuple[datetime.date, List[StockQuote]]] = [
        (day(offset), [make_quote(day(offset), 100.0, high=101.0, low=99.0)])
        for offset in range(10)
    ]
    margin_swing_bars.append(
        (day(10), [make_quote(day(10), 95.0, high=101.0, low=94.0)])
    )
    scenarios.append(
        ShortScenario(
            name="margin_swing_10_days",
            verifies="保證金佔用、融券利息於平倉一次計算、holding_days 累計",
            strategy=make_short_strategy(
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 2)]},
                close_script={
                    day(10): [make_short_order(day(10), Action.BUY, 95.0, 2)]
                },
            ),
            bars=margin_swing_bars,
        )
    )

    # === 3. 開兩筆、只回補一部分：FIFO 方向篩選 ＋ 等比例攤提 ===
    scenarios.append(
        ShortScenario(
            name="partial_cover_fifo",
            verifies="FIFO 跨部位拆單、第二筆部位的等比例攤提、殘量留倉",
            strategy=make_short_strategy(
                open_script={
                    day(0): [make_short_order(day(0), Action.SELL, 100.0, 3)],
                    day(1): [make_short_order(day(1), Action.SELL, 105.0, 2)],
                },
                close_script={day(2): [make_short_order(day(2), Action.BUY, 95.0, 4)]},
            ),
            bars=[
                (day(0), [make_quote(day(0), 100.0, high=101.0, low=99.0)]),
                (day(1), [make_quote(day(1), 105.0, high=106.0, low=99.0)]),
                (day(2), [make_quote(day(2), 95.0, high=106.0, low=94.0)]),
            ],
        )
    )

    # === 4. 持有中股價上漲觸發維持率：斷頭強制回補 ===
    scenarios.append(
        ShortScenario(
            name="margin_call_force_cover",
            verifies="維持率跌破門檻的強制回補與事件計數",
            strategy=make_short_strategy(
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 2)]},
            ),
            bars=[
                (day(0), [make_quote(day(0), 100.0, high=101.0, low=99.0)]),
                # 漲到 150：維持率 380000 / 300000 = 126.7% < 130%
                (day(1), [make_quote(day(1), 150.0, high=151.0, low=149.0)]),
            ],
        )
    )

    # === 5. 當沖日終鎖漲停：轉融券留倉、事件計數 ===
    scenarios.append(
        ShortScenario(
            name="limit_up_locked_convert",
            verifies="漲停鎖死無法回補 → 轉融券留倉、補收保證金與券費",
            strategy=make_short_strategy(
                enable_intraday=True,
                open_script={day(1): [make_short_order(day(1), Action.SELL, 110.0, 1)]},
            ),
            bars=[
                # 第一根 bar 不下單，只為了讓引擎記下前收 100（漲停價才會是 110）
                (day(0), [make_quote(day(0), 100.0)]),
                (day(1), [make_quote(day(1), 110.0)]),
            ],
        )
    )

    # === 6. 當沖 ＋ 停券強制回補日：釘住兩個收盤後動作的先後順序 ===
    #
    # enforce_day_trade_cover() 與 execute_daily_position_check() 已被
    # 合併進 SettlementModel.on_bar_close()，兩者對調不會讓任何單元測試失敗，
    # 但會讓同一次強制回補記到不同的事件桶。此情境是唯一能抓到該漂移的護欄：
    # - 現行順序：當沖回補先執行 → forced_cover_day_trade
    # - 順序對調：停券檢查先執行 → forced_cover_max_holding
    scenarios.append(
        ShortScenario(
            name="day_trade_on_force_cover_date",
            verifies="enforce_day_trade_cover 必須早於 execute_daily_position_check",
            strategy=make_short_strategy(
                enable_intraday=True,
                short_constraint=ShortConstraint(
                    force_cover_dates={STOCK_ID: [day(0)]}
                ),
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 1)]},
            ),
            bars=[(day(0), [make_quote(day(0), 98.0, high=101.0, low=97.0)])],
        )
    )

    # === 7. 借券費計提：只有 SBL 逐日累加，MARGIN 恆為 0（不重複計費）===
    for short_method in (ShortMethod.SBL, ShortMethod.MARGIN):
        borrow_fee_bars: List[Tuple[datetime.date, List[StockQuote]]] = [
            (day(offset), [make_quote(day(offset), 100.0, high=101.0, low=99.0)])
            for offset in range(6)
        ]
        scenarios.append(
            ShortScenario(
                name=f"borrow_fee_{short_method.value.lower()}",
                verifies="SBL 逐日計提借券費；MARGIN 於開倉一次收取、accrued 恆為 0",
                strategy=make_short_strategy(
                    short_method=short_method,
                    open_script={
                        day(0): [make_short_order(day(0), Action.SELL, 100.0, 1)]
                    },
                    close_script={
                        day(5): [make_short_order(day(5), Action.BUY, 100.0, 1)]
                    },
                ),
                bars=borrow_fee_bars,
            )
        )

    # === 8. 除權息前的融券最後回補日：MARGIN 被強制回補、SBL 不受影響 ===
    #
    # 回補日由 `TwStockDataFeed` 依「除權息交易日前 4 個營業日」推導，此處直接以
    # 腳本給定。兩個管道跑同一份腳本，釘住「自動推導的停券日只對融券生效」——
    # 若哪天誤把 SBL 也納入，第二組情境的期末部位會直接消失。
    for short_method in (ShortMethod.MARGIN, ShortMethod.SBL):
        scenarios.append(
            ShortScenario(
                name=f"ex_dividend_force_cover_{short_method.value.lower()}",
                verifies="除權息推導的停券日只強制回補 MARGIN，SBL 續抱",
                strategy=make_short_strategy(
                    short_method=short_method,
                    open_script={
                        day(0): [make_short_order(day(0), Action.SELL, 100.0, 2)]
                    },
                ),
                bars=[
                    (day(0), [make_quote(day(0), 100.0, high=101.0, low=99.0)]),
                    (day(1), [make_quote(day(1), 99.0, high=100.0, low=98.0)]),
                ],
                force_cover_script={day(1): {STOCK_ID}},
            )
        )

    # === 9. SBL 留倉跨除息日：補償出借方現金股利 ===
    #
    # 股利補償是 carry cost 的一部分，會同時出現在 record 的
    # Dividend Compensation、Transaction Cost 與 Realized PnL 三處，
    # 任何一處漏掉都會讓三者對不起來。
    scenarios.append(
        ShortScenario(
            name="ex_dividend_compensation_sbl",
            verifies="跨除息日的 SBL 空單補償現金股利，並攤提進已實現損益",
            strategy=make_short_strategy(
                short_method=ShortMethod.SBL,
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 2)]},
                close_script={day(2): [make_short_order(day(2), Action.BUY, 98.0, 2)]},
            ),
            bars=[
                (day(0), [make_quote(day(0), 100.0, high=101.0, low=99.0)]),
                # 除息 2 元／股：股價跳空到 98，空單帳面賺 4,000，同額補償給出借方
                (day(1), [make_quote(day(1), 98.0, high=99.0, low=97.0)]),
                (day(2), [make_quote(day(2), 98.0, high=99.0, low=97.0)]),
            ],
            cash_dividend_script={day(1): {STOCK_ID: 2.0}},
        )
    )

    # === 10. 部分回補時的股利補償等比例攤提 ===
    scenarios.append(
        ShortScenario(
            name="ex_dividend_compensation_partial_cover",
            verifies="股利補償依回補張數攤提，剩餘部位保留未攤提的部分",
            strategy=make_short_strategy(
                short_method=ShortMethod.SBL,
                open_script={day(0): [make_short_order(day(0), Action.SELL, 100.0, 4)]},
                close_script={day(2): [make_short_order(day(2), Action.BUY, 98.0, 1)]},
            ),
            bars=[
                (day(0), [make_quote(day(0), 100.0, high=101.0, low=99.0)]),
                (day(1), [make_quote(day(1), 98.0, high=99.0, low=97.0)]),
                (day(2), [make_quote(day(2), 98.0, high=99.0, low=97.0)]),
            ],
            cash_dividend_script={day(1): {STOCK_ID: 1.5}},
        )
    )

    return scenarios


def run_scenario(scenario: ShortScenario) -> Backtester:
    """逐 bar 跑完一組情境，回傳跑完的引擎"""

    backtester: Backtester = build_scripted_backtester(scenario.strategy)
    backtester.data_feed = ScriptedDataFeed(
        force_cover_script=scenario.force_cover_script,
        cash_dividend_script=scenario.cash_dividend_script,
    )

    for date, stock_quotes in scenario.bars:
        backtester.execute_bar(date, stock_quotes)

    backtester.account.update_account_status()
    return backtester


def collect_trade_rows(
    scenario_name: str, backtester: Backtester
) -> List[Dict[str, Any]]:
    """取出已平倉的交易紀錄全欄位（放空記帳的主要驗收對象）"""

    rows: List[Dict[str, Any]] = []

    for record in backtester.account.trade_records:
        if not record.is_closed:
            continue

        rows.append(
            {
                "Scenario": scenario_name,
                "Trade ID": record.id,
                "Symbol": record.symbol,
                "Position Type": record.position_type.value,
                "Short Method": record.short_method.value
                if record.short_method
                else "",
                "Entry Date": str(record.entry_date),
                "Entry Price": record.entry_price,
                "Exit Date": str(record.exit_date),
                "Exit Price": record.exit_price,
                "Sell Date": str(record.sell_date),
                "Sell Price": record.sell_price,
                "Sell Volume": record.sell_volume,
                "Buy Date": str(record.buy_date),
                "Buy Price": record.buy_price,
                "Buy Volume": record.buy_volume,
                "Commission": record.commission,
                "Tax": record.tax,
                "Borrow Fee": record.borrow_fee,
                "Interest": record.interest,
                "Dividend Compensation": record.dividend_compensation,
                "Margin": record.margin,
                "Transaction Cost": record.transaction_cost,
                "Holding Days": record.holding_days,
                "Realized PnL": record.realized_pnl,
                "ROI": record.roi,
                "ROI on Capital": record.roi_on_capital,
            }
        )

    return rows


def collect_position_rows(
    scenario_name: str, backtester: Backtester
) -> List[Dict[str, Any]]:
    """取出期末未平倉部位：轉融券留倉與逐日計提的結果只存在於此"""

    rows: List[Dict[str, Any]] = []

    for position in backtester.account.get_positions():
        rows.append(
            {
                "Scenario": scenario_name,
                "Position ID": position.id,
                "Symbol": position.symbol,
                "Position Type": position.position_type.value,
                "Short Method": position.short_method.value
                if position.short_method
                else "",
                "Is Day Trade": position.is_day_trade,
                "Open Date": str(position.date),
                "Open Price": position.price,
                "Volume": position.volume,
                "Commission": position.commission,
                "Tax": position.tax,
                "Transaction Cost": position.transaction_cost,
                "Margin": position.margin,
                "Short Proceeds": position.short_proceeds,
                "Borrow Fee": position.borrow_fee,
                "Accrued Borrow Fee": position.accrued_borrow_fee,
                "Dividend Compensation": position.dividend_compensation,
                "Holding Days": position.holding_days,
            }
        )

    return rows


def collect_summary_row(scenario_name: str, backtester: Backtester) -> Dict[str, Any]:
    """取出帳戶終值與全部事件計數（報表相容性的驗收對象）"""

    account = backtester.account

    row: Dict[str, Any] = {
        "Scenario": scenario_name,
        "Balance": account.balance,
        "Margin Used": account.margin_used,
        "Realized PnL": account.realized_pnl,
        "ROI": account.roi,
        "Total Commission": account.total_commission,
        "Total Tax": account.total_tax,
        "Total Transaction Cost": account.total_transaction_cost,
        "Trade Count": len(account.trade_records),
        "Open Position Count": len(account.get_positions()),
    }

    # event_counts 的既有 key 在重構後必須維持不變（報表相容）
    for event, count in backtester.event_counts.items():
        row[event] = count

    return row


def generate_baseline() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """跑完所有情境並輸出三份快照，作為 SHORT 路徑的回歸基準"""

    trade_rows: List[Dict[str, Any]] = []
    position_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for scenario in build_scenarios():
        backtester: Backtester = run_scenario(scenario)

        trade_rows.extend(collect_trade_rows(scenario.name, backtester))
        position_rows.extend(collect_position_rows(scenario.name, backtester))
        summary_rows.append(collect_summary_row(scenario.name, backtester))

    trades: pd.DataFrame = pd.DataFrame(trade_rows)
    positions: pd.DataFrame = pd.DataFrame(position_rows)
    summary: pd.DataFrame = pd.DataFrame(summary_rows)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    trades.to_csv(SNAPSHOT_DIR / BASELINE_FILE_NAME, index=False)
    positions.to_csv(SNAPSHOT_DIR / POSITION_FILE_NAME, index=False)
    summary.to_csv(SNAPSHOT_DIR / SUMMARY_FILE_NAME, index=False)

    print(f"* Baseline saved: {SNAPSHOT_DIR / BASELINE_FILE_NAME}")
    print(f"* Scenarios: {len(summary)}")
    print(f"* Trades: {len(trades)}")
    print(f"* Open positions: {len(positions)}")
    return trades, positions, summary


if __name__ == "__main__":
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Generate SHORT regression baseline"
    )
    parser.parse_args()

    generate_baseline()
