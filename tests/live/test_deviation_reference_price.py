from typing import List

from core.live.trader import quote_prices_of
from core.models import StockQuote
from core.utils import ExecutionStyle, ExecutionTiming, Scale

from .test_live_execution_layer import events, sent_orders
from .test_live_trader_day import TODAY, FakeFeed, Harness, ScriptedStrategy, make_order

"""
決策價偏離檢查的市場基準價

偏離檢查要擋的是「策略算出來的價格離市場太遠」。基準價若取自這張單自己
（決策價或委託價），就是自己比自己、偏離恆為 0，`RiskConfig.price_deviation_ratio`
在實盤形同不存在。這裡盯住基準價真的來自市場報價，以及取不到時的行為。
"""


class Decides(ScriptedStrategy):
    """要成交的開倉買單，決策價由測試指定；市場報價固定 100"""

    def __init__(self, price: float) -> None:
        super().__init__("Decides", [make_order(price=price)])
        self.live_execution = ExecutionStyle.MARKET


def test_decision_price_far_from_the_market_is_rejected() -> None:
    """市場 100、決策 108：偏離 8% 超過預設 3%，擋下並寫 PRICE_DEVIATION"""

    harness: Harness = Harness([Decides(108.0)])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 0
    assert events(harness, "PRICE_DEVIATION") == [("Decides", "2330", "WARN")]


def test_decision_price_near_the_market_is_sent() -> None:
    """市場 100、決策 102：偏離 2% 在上限內，照常送出（掛漲停 110）"""

    harness: Harness = Harness([Decides(102.0)])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    (order,) = sent_orders(harness)
    assert (order.decision_price, order.price) == (102.0, 110.0)
    assert events(harness, "PRICE_DEVIATION") == []


def test_missing_reference_price_lets_the_order_through_with_a_warning() -> None:
    """本段落沒有該檔報價、合約也查不到參考價：放行，但要留下沒檢查到的紀錄"""

    harness: Harness = Harness([Decides(108.0)])
    harness.trader.contexts[0].data_feed = FakeFeed([])
    harness.trader.run(ExecutionTiming.AT_CLOSE)

    assert harness.broker.placed_count == 1
    assert events(harness, "PRICE_DEVIATION") == []
    assert events(harness, "REFERENCE_PRICE_MISSING") == [("Decides", "2330", "WARN")]


def test_quote_prices_prefer_the_last_trade_and_skip_empty_quotes() -> None:
    """最新成交價優先、沒有才退回收盤；0 或沒有代號的報價不算"""

    quotes: List[StockQuote] = [
        StockQuote(
            stock_id="2330", scale=Scale.DAY, date=TODAY, cur_price=101.0, close=99.0
        ),
        StockQuote(
            stock_id="2317", scale=Scale.DAY, date=TODAY, cur_price=0.0, close=50.0
        ),
        StockQuote(
            stock_id="0000", scale=Scale.DAY, date=TODAY, cur_price=0.0, close=0.0
        ),
    ]

    assert quote_prices_of(quotes) == {"2330": 101.0, "2317": 50.0}
