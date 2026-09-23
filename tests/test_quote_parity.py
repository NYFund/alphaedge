import datetime
import queue
from types import SimpleNamespace
from typing import Any, List, Optional

import pandas as pd
import pytest

from core.adapters.tw.stock_quote_adapter import StockQuoteAdapter
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.models import StockQuote
from core.utils import Scale, Units

"""
回測與實盤兩條報價轉換路徑的**口徑**守門

同一支策略在回測與實盤讀的是同一個 `StockQuote`，但它是由兩條完全獨立的程式
組出來的：回測走 `StockQuoteAdapter`（讀 SQLite 的價格表），實盤走
`ShioajiQuoteStream.to_stock_quote()`（讀券商快照）。兩邊各自演化時，
**欄位口徑漂移不會有任何錯誤訊息**，只會讓同一支策略在實盤算出不同訊號。

本檔只比對口徑，不比對數值——兩邊的資料來源本來就不同。

〈不變式：兩邊必須一致，不一致就是錯〉

| 欄位 | 回測路徑 | 實盤路徑 | 不變式 |
|------|----------|----------|--------|
| `volume` | `成交股數` ÷ `Units.LOT` | `snapshot.total_volume` | 單位都是**張**（`StockQuote.volume` 標註 `Unit: Lot`） |
| `cur_price` | ＝ `close` | ＝ `close` | 兩者恆等 |
| `adj_close` | 呼叫端給值，未啟用還原時為 `None` | 一律 `None` | 缺值是 `None` 而不是 `0` |
| `signal_close` | `adj_close` 為 `None` 時退回 `close` | 同左 | 退回規則相同 |
| `scale` | `Scale.DAY` | `Scale.DAY` | 相同 |

〈制度性差異：刻意不同，**不該被測試抹平**〉

| 欄位 | 差異 |
|------|------|
| `close` | 回測是當日收盤（定值）；實盤是盤中最新成交價（**會變**）。策略若拿它算「今天漲幅」，實盤得到的是此刻漲幅。 |
| `open`/`high`/`low` | 回測是當日 OHLC；實盤是盤中累計值。盤前段落另走 `PreOpenStockQuote`（讀 OHLC 直接拋）。 |

〈最容易漂掉的那一個〉

實盤的 `volume` 必須取 `snapshot.total_volume`（當日累計）而不是 `snapshot.volume`
（該筆成交量）。取錯不會報錯，只會讓「當日成交量 ≥ N 張」這類門檻永遠不成立——
策略在實盤一張單都不會送出，而回測看起來一切正常。
"""


STOCK_ID: str = "2330"
DATE: datetime.date = datetime.date(2024, 1, 2)

# 同一檔、同一天的假資料，兩邊各自餵進自己的轉換路徑
CLOSE: float = 600.0
OPEN: float = 590.0
HIGH: float = 605.0
LOW: float = 588.0
LOTS: int = 12_345  # 張
SHARES: int = LOTS * Units.LOT  # 回測來源是股數


def backtest_quote(adj_close: Optional[float] = None) -> StockQuote:
    """走回測路徑：價格表的一列 → StockQuote"""

    price_df: pd.DataFrame = pd.DataFrame(
        [
            {
                "date": DATE.isoformat(),
                "stock_id": STOCK_ID,
                "開盤價": OPEN,
                "最高價": HIGH,
                "最低價": LOW,
                "收盤價": CLOSE,
                "成交股數": SHARES,
            }
        ]
    )
    quotes: List[StockQuote] = StockQuoteAdapter.from_day_rows(
        price_df,
        DATE,
        adjusted_close_map={STOCK_ID: adj_close} if adj_close is not None else None,
    )
    assert len(quotes) == 1, "測試資料只有一檔，轉換不該增減列"
    return quotes[0]


def make_snapshot(**overrides: Any) -> SimpleNamespace:
    """券商快照的最小替身；`ts` 的語意見 `ShioajiQuoteStream._resolve_date()`"""

    fields: dict = {
        "code": STOCK_ID,
        "close": CLOSE,
        "open": OPEN,
        "high": HIGH,
        "low": LOW,
        # 當日累計成交量（張）
        "total_volume": LOTS,
        # 該筆成交量——**不可被誤用**，本檔的核心斷言就是這件事
        "volume": 7,
        "ts": int(datetime.datetime(2024, 1, 2, 13, 30).timestamp() * 1_000_000_000),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def stream() -> ShioajiQuoteStream:
    """不連券商的行情串流；本檔只用它的轉換方法"""

    return ShioajiQuoteStream(
        api=SimpleNamespace(),
        rate_limiter=SimpleNamespace(acquire=lambda *args, **kwargs: None),
        quote_queue=queue.Queue(),
    )


def live_quote(stream: ShioajiQuoteStream, **overrides: Any) -> StockQuote:
    """走實盤路徑：券商快照 → StockQuote"""

    return stream.to_stock_quote(make_snapshot(**overrides))


# === 不變式：兩邊必須一致 ===
def test_volume_is_in_lots_on_both_paths(stream: ShioajiQuoteStream) -> None:
    """
    兩邊的 `volume` 單位都是張

    `StockQuote.volume` 標註 `Unit: Lot`，但回測的來源是**股數**、實盤的來源是
    **張**，各自換算。少一次換算不會報錯，只會讓張數差 1000 倍——
    而「成交量 ≥ 5000 張」這類門檻會變成永遠成立或永遠不成立。
    """

    assert backtest_quote().volume == LOTS
    assert live_quote(stream).volume == LOTS


def test_live_volume_comes_from_total_volume(stream: ShioajiQuoteStream) -> None:
    """
    實盤取的是**當日累計**成交量，不是該筆成交量

    這是本檔最容易漂掉的一條：`snapshot.volume` 與 `snapshot.total_volume`
    只差一個前綴，取錯不會報錯，策略在實盤一張單都不會送出。
    """

    quote: StockQuote = live_quote(stream, total_volume=8_888, volume=7)

    assert quote.volume == 8_888
    assert quote.volume != 7, "取到 snapshot.volume（該筆成交量）了"


def test_cur_price_equals_close_on_both_paths(stream: ShioajiQuoteStream) -> None:
    """
    兩邊的 `cur_price` 都等於 `close`

    策略有些地方讀 `cur_price`、有些讀 `close`。兩者一旦在某一條路徑上分家，
    同一支策略在兩邊會看到不同的價格，而且沒有任何地方會報錯。
    """

    backtest: StockQuote = backtest_quote()
    live: StockQuote = live_quote(stream)

    assert backtest.cur_price == backtest.close
    assert live.cur_price == live.close


def test_adj_close_defaults_to_none_not_zero(stream: ShioajiQuoteStream) -> None:
    """
    沒有還原價時是 `None` 而不是 `0`

    填 0 會讓「沒有還原價」與「還原價是 0」混為一談，而下游的
    `signal_close` 一旦拿到 0，漲跌幅會變成 -100%。
    """

    assert backtest_quote().adj_close is None
    assert live_quote(stream).adj_close is None


def test_signal_close_falls_back_to_close_on_both_paths(
    stream: ShioajiQuoteStream,
) -> None:
    """未啟用還原時，兩邊的 `signal_close` 都退回 `close`"""

    assert backtest_quote().signal_close == CLOSE
    assert live_quote(stream).signal_close == CLOSE


def test_adjusted_close_only_touches_signal_close() -> None:
    """
    啟用還原價時只有 `signal_close` 改變，OHLC 與 `close` 維持原始成交價

    稅費、漲跌停與檔位判定都是對**實際成交金額**課徵的，用還原價會算錯。
    """

    quote: StockQuote = backtest_quote(adj_close=550.0)

    assert quote.signal_close == 550.0
    assert quote.close == CLOSE
    assert quote.cur_price == CLOSE
    assert (quote.open, quote.high, quote.low) == (OPEN, HIGH, LOW)


def test_scale_is_day_on_both_paths(stream: ShioajiQuoteStream) -> None:
    """兩邊都標成日線；`scale` 決定下游怎麼解讀這筆報價"""

    assert backtest_quote().scale == Scale.DAY
    assert live_quote(stream).scale == Scale.DAY


def test_symbol_is_the_stock_id_on_both_paths(stream: ShioajiQuoteStream) -> None:
    """`symbol` 是對帳與 parity 比對的鍵，兩邊必須是同一個代號"""

    assert backtest_quote().symbol == STOCK_ID
    assert live_quote(stream).symbol == STOCK_ID


# === 制度性差異：刻意不同，不可被抹平 ===
def test_live_close_is_a_running_price(stream: ShioajiQuoteStream) -> None:
    """
    實盤的 `close` 是盤中最新成交價，會隨每次快照改變

    這是**刻意的差異**，不是缺陷：盤中本來就還沒收盤。
    釘住它是為了讓「策略拿 close 算今天漲幅」的人知道自己在實盤讀到的是什麼。
    """

    first: StockQuote = live_quote(stream, close=600.0)
    second: StockQuote = live_quote(stream, close=612.0)

    assert first.close == 600.0
    assert second.close == 612.0
    assert first.close != second.close


def test_snapshot_without_a_price_is_skipped_not_zeroed(
    stream: ShioajiQuoteStream,
) -> None:
    """
    快照沒有有效成交價時**整檔略過**，不產出 0 元報價

    改動前這裡會回一個 `close=0` 的 `StockQuote`，而回測那一側是直接濾掉
    這檔的（`has_valid_price()`）。兩邊不一致的症狀是實盤拿 0 元算訊號與
    成交價，且不會有任何錯誤——這正是兩條路徑共用同一層驗證要防的事。
    """

    assert stream.to_stock_quote(make_snapshot(close=None)) is None
    assert stream.to_stock_quote(make_snapshot(close=0.0)) is None
    assert stream.to_stock_quote(make_snapshot(close=-1.0)) is None


def test_valid_snapshot_still_produces_a_quote(stream: ShioajiQuoteStream) -> None:
    """有有效價時照常產出——略過的判準不可寬到把正常報價也濾掉"""

    quote: Optional[StockQuote] = stream.to_stock_quote(make_snapshot(close=600.0))

    assert quote is not None
    assert quote.close == 600.0
