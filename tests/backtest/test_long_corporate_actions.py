import datetime
from typing import Dict, Optional

from core.backtest.models.cost_model import StockCostModel
from core.backtest.models.settlement_model import TwStockSettlementModel
from core.managers.stock.position_manager import StockPositionManager
from core.models import StockAccount, StockPosition, StockQuote
from core.models.cost_config import CostConfig
from core.utils import PositionType, Scale

"""
做多部位的公司行動記帳

**做多路徑原本完全不處理公司行動**，而盯市用的是未還原價：跨除息時收盤價跳空
−3 元、帳戶卻沒收到那 3 元；跨配股或分割時張數不變、價格砍半，帳面憑空虧一半。
放空跨配股則反向憑空獲利。還原價只解決訊號面的問題，記帳面要另外做。

第二件事是**長期無報價**：下市或停牌的股票不再出現在 `price` 表，引擎不會把
無報價的部位交給策略，於是它永遠留在帳上、以最後一個收盤價計入權益
（存活者偏差），還一直佔著 `max_holdings` 的名額。
"""

STOCK_ID: str = "2330"
EX_DATE: datetime.date = datetime.date(2024, 1, 4)
OPEN_DATE: datetime.date = datetime.date(2024, 1, 2)


def new_event_counts() -> Dict[str, int]:
    """事件計數桶（缺鍵時預設為 0）"""

    from collections import defaultdict

    return defaultdict(int)


def make_settlement(
    account: StockAccount,
    max_no_quote_days: Optional[int] = None,
) -> TwStockSettlementModel:
    """組出台股結算模型"""

    cost_model: StockCostModel = StockCostModel(CostConfig.default())
    return TwStockSettlementModel(
        position_manager=StockPositionManager(account, cost_model),
        cost_model=cost_model,
        prev_close={},
        max_no_quote_days=max_no_quote_days,
    )


def make_long_position(
    price: float = 100.0,
    volume: int = 2,
    date: datetime.date = OPEN_DATE,
) -> StockPosition:
    """建立做多部位"""

    return StockPosition(
        id=1,
        stock_id=STOCK_ID,
        position_type=PositionType.LONG,
        date=date,
        price=price,
        volume=volume,
    )


def make_account(position: Optional[StockPosition] = None) -> StockAccount:
    """建立持有一筆做多部位的帳戶"""

    account: StockAccount = StockAccount(1000000.0)
    account.positions.append(position or make_long_position())
    return account


def make_quote(close: float) -> StockQuote:
    """當日報價"""

    return StockQuote(
        stock_id=STOCK_ID,
        scale=Scale.DAY,
        date=EX_DATE,
        cur_price=close,
        volume=1000,
        open=close,
        high=close,
        low=close,
        close=close,
    )


# === 現金股利 ===
def test_long_position_receives_cash_dividend() -> None:
    """跨除息的做多部位收到現金股利，同額入帳"""

    account: StockAccount = make_account()
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 3.0})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.settle_cash_dividend(EX_DATE, account, event_counts)

    # 3 元／股 × 2 張 × 1000 股
    assert account.positions[0].dividend_received == 6000
    assert account.balance == 1000000.0 + 6000
    assert event_counts["dividend_received"] == 1


def test_long_position_opened_on_ex_date_gets_nothing() -> None:
    """除權息交易日當天買進者不含權"""

    account: StockAccount = make_account(make_long_position(date=EX_DATE))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 3.0})

    settlement.settle_cash_dividend(EX_DATE, account, new_event_counts())

    assert account.positions[0].dividend_received == 0
    assert account.balance == 1000000.0


def test_equity_is_continuous_across_the_ex_dividend_day() -> None:
    """
    **除息日的權益不再跳空**（本步驟的驗收條件）

    收盤價跌 3 元、帳戶收到 3 元／股，兩者相抵；沒有股利入帳的話，
    權益會在除息當日整段掉下來，而那不是虧損。
    """

    account: StockAccount = make_account()
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_cash_dividends({STOCK_ID: 3.0})

    position: StockPosition = account.positions[0]
    units: int = settlement.instrument.to_units(position.volume)
    before: float = account.balance + settlement.mark_position(position, 100.0, units)

    settlement.settle_cash_dividend(EX_DATE, account, new_event_counts())
    after: float = account.balance + settlement.mark_position(position, 97.0, units)

    assert after == before


# === 配股、分割、減資 ===
def test_stock_dividend_adjusts_volume_and_cost() -> None:
    """配股：股數乘以倍率、每股成本除以倍率，成本總額不變"""

    account: StockAccount = make_account(make_long_position(price=100.0, volume=2))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_share_ratios({STOCK_ID: 1.5})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.apply_corporate_actions(EX_DATE, account, event_counts)

    position: StockPosition = account.positions[0]
    assert position.volume == 3
    # 每股成本存到小數第 4 位，避免浮點尾數在報表裡漂
    assert position.price == round(100.0 / 1.5, 4)
    assert event_counts["share_adjustment_applied"] == 1


def test_unknown_share_ratio_is_counted_not_guessed() -> None:
    """
    配股率未知時不調整股數，但要記 warning 並計數

    以前 NULL 被當成 0 靜靜跳過，報表看不出哪些部位少算了配股；
    現金股利 NULL 早就是「不猜、記 warning、計數」，兩者口徑一致。
    """

    account: StockAccount = make_account(make_long_position(price=100.0, volume=2))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_share_ratios({STOCK_ID: float("nan")})
    event_counts: Dict[str, int] = new_event_counts()

    settlement.apply_corporate_actions(EX_DATE, account, event_counts)

    assert account.positions[0].volume == 2
    assert event_counts["share_adjustment_unknown"] == 1
    assert event_counts["share_adjustment_applied"] == 0


def test_split_keeps_the_equity_continuous() -> None:
    """
    分割：張數加倍、價格砍半時權益不變

    不調整的話帳面會憑空虧一半，而那只是面額改變。
    """

    account: StockAccount = make_account(make_long_position(price=100.0, volume=2))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_share_ratios({STOCK_ID: 2.0})

    position: StockPosition = account.positions[0]
    units: int = settlement.instrument.to_units(position.volume)
    before: float = account.balance + settlement.mark_position(position, 100.0, units)

    settlement.apply_corporate_actions(EX_DATE, account, new_event_counts())
    after: float = account.balance + settlement.mark_position(
        position, 50.0, settlement.instrument.to_units(position.volume)
    )

    assert position.volume == 4
    assert after == before


def test_odd_shares_are_converted_to_cash() -> None:
    """不足一張的零股折成現金入帳，不可四捨五入吞掉"""

    account: StockAccount = make_account(make_long_position(price=100.0, volume=1))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_share_ratios({STOCK_ID: 1.05})

    settlement.apply_corporate_actions(EX_DATE, account, new_event_counts())

    position: StockPosition = account.positions[0]
    # 1,050 股 → 1 張 ＋ 50 股零股；零股以調整後的每股成本折現
    assert position.volume == 1
    assert account.balance == 1000000.0 + int(50 * (100.0 / 1.05))


def test_position_opened_on_the_action_date_is_not_adjusted() -> None:
    """當天才買進的部位已是調整後的價格，不再調整"""

    account: StockAccount = make_account(make_long_position(date=EX_DATE))
    settlement: TwStockSettlementModel = make_settlement(account)
    settlement.apply_share_ratios({STOCK_ID: 2.0})

    settlement.apply_corporate_actions(EX_DATE, account, new_event_counts())

    assert account.positions[0].volume == 2


# === 長期無報價 ===
def test_long_position_exits_after_the_no_quote_limit() -> None:
    """做多部位連續無報價達上限時強制出場並計數"""

    account: StockAccount = make_account()
    settlement: TwStockSettlementModel = make_settlement(account, max_no_quote_days=3)
    settlement.prev_close[STOCK_ID] = 90.0
    event_counts: Dict[str, int] = new_event_counts()

    # 連續三天沒有報價
    for _ in range(3):
        settlement.update_no_quote_days({}, account.get_positions())

    settlement.check_long_no_quote_exit(EX_DATE, {}, account, event_counts)

    assert event_counts["forced_exit_no_quote"] == 1
    assert account.get_positions() == []


def test_long_position_with_quotes_is_kept() -> None:
    """有報價就歸零計數，不會被誤出場（防止改過頭）"""

    account: StockAccount = make_account()
    settlement: TwStockSettlementModel = make_settlement(account, max_no_quote_days=1)
    event_counts: Dict[str, int] = new_event_counts()

    settlement.update_no_quote_days(
        {STOCK_ID: make_quote(100.0)}, account.get_positions()
    )
    settlement.check_long_no_quote_exit(EX_DATE, {}, account, event_counts)

    assert event_counts["forced_exit_no_quote"] == 0
    assert len(account.get_positions()) == 1
