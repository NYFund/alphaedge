import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from core.models import BaseQuote

"""
把錄製的行情重放回來

**重放一定要走真正的轉換路徑**（`ShioajiQuoteStream.from_tick_message()`），
否則驗到的只是「JSON 讀得出來」，而不是「這份資料餵進系統會得到什麼」。
故本模組只負責把 JSON 還原成**與券商推播同形狀的物件**，轉換交給原本那一份。

**型別要照著還原**：JSON 沒有 `Decimal` 與 `datetime`，錄製時它們被序列化成字串。
直接把字串餵進轉換層，`float('4930')` 雖然會過，但 `datetime` 那一欄會當場炸——
而更糟的情況是某天有人加了 `.get()` 預設值，於是重放靜靜地產出錯的報價。
型別表寫死在本模組（`RECORDED_FIELD_TYPES`），描述的是**錄製檔的格式**，不是某一版
shioaji 的型別：shioaji 1.7 的 `shioaji.stream_data_type` 只剩轉接層、讀不到任何標註，
改成動態讀標註會讓重放的 `datetime` 停在字串。錄製檔一旦寫下就不會變，
型別表也不該跟著安裝的套件浮動。
"""

_TICK_STK_FIELDS: Dict[str, str] = {
    "code": "str",
    "datetime": "datetime",
    "open": "Decimal",
    "avg_price": "Decimal",
    "close": "Decimal",
    "high": "Decimal",
    "low": "Decimal",
    "amount": "Decimal",
    "total_amount": "Decimal",
    "volume": "int",
    "total_volume": "int",
    "tick_type": "int",
    "chg_type": "int",
    "price_chg": "Decimal",
    "pct_chg": "Decimal",
    "bid_side_total_vol": "int",
    "ask_side_total_vol": "int",
    "bid_side_total_cnt": "int",
    "ask_side_total_cnt": "int",
    "closing_oddlot_shares": "int",
    "fixed_trade_vol": "int",
    "suspend": "bool",
    "simtrade": "bool",
    "intraday_odd": "bool",
}

_BIDASK_STK_FIELDS: Dict[str, str] = {
    "code": "str",
    "datetime": "datetime",
    "bid_price": "List[Decimal]",
    "bid_volume": "List[int]",
    "diff_bid_vol": "List[int]",
    "ask_price": "List[Decimal]",
    "ask_volume": "List[int]",
    "diff_ask_vol": "List[int]",
    "suspend": "bool",
    "simtrade": "bool",
    "intraday_odd": "bool",
}

_TICK_FOP_FIELDS: Dict[str, str] = {
    "code": "str",
    "datetime": "datetime",
    "open": "Decimal",
    "underlying_price": "Decimal",
    "bid_side_total_vol": "int",
    "ask_side_total_vol": "int",
    "avg_price": "Decimal",
    "close": "Decimal",
    "high": "Decimal",
    "low": "Decimal",
    "amount": "Decimal",
    "total_amount": "Decimal",
    "volume": "int",
    "total_volume": "int",
    "tick_type": "int",
    "chg_type": "int",
    "price_chg": "Decimal",
    "pct_chg": "Decimal",
    "simtrade": "bool",
}

_BIDASK_FOP_FIELDS: Dict[str, str] = {
    "code": "str",
    "datetime": "datetime",
    "bid_total_vol": "int",
    "ask_total_vol": "int",
    "bid_price": "List[Decimal]",
    "bid_volume": "List[int]",
    "diff_bid_vol": "List[int]",
    "ask_price": "List[Decimal]",
    "ask_volume": "List[int]",
    "diff_ask_vol": "List[int]",
    "first_derived_bid_price": "Decimal",
    "first_derived_ask_price": "Decimal",
    "first_derived_bid_vol": "int",
    "first_derived_ask_vol": "int",
    "underlying_price": "Decimal",
    "simtrade": "bool",
}

# 訊息類別 → 欄位 → 型別名。欄位與型別取自 shioaji 1.3.3 的 `stream_data_type` 標註
# （既有錄製檔的格式），錄製腳本也以此決定要錄哪些欄位
RECORDED_FIELD_TYPES: Dict[str, Dict[str, str]] = {
    "TickSTKv1": _TICK_STK_FIELDS,
    "BidAskSTKv1": _BIDASK_STK_FIELDS,
    "TickFOPv1": _TICK_FOP_FIELDS,
    "BidAskFOPv1": _BIDASK_FOP_FIELDS,
}

# 錄製時的 `kind` → 對應的訊息類別
KIND_TO_TYPE: Dict[str, str] = {
    "tick_stk": "TickSTKv1",
    "bidask_stk": "BidAskSTKv1",
    "tick_fop": "TickFOPv1",
    "bidask_fop": "BidAskFOPv1",
}


class RecordedMessage:
    """
    - Description:
        錄製下來的一筆行情，形狀與券商推播的物件一致

        **以 `__getattr__` 提供欄位而不是先塞進 `__dict__`**：真品是原生擴充物件，
        沒有 `__dict__`、`dict()`、`model_dump()`，只能照欄位名取值。
        重放物件若長得比真品「好用」，轉換層就可能在重放時走到一條真實環境
        走不到的路。
    """

    def __init__(self, payload: Dict[str, Any], type_name: str) -> None:
        """
        - Description:
            建立重放訊息
        - Parameters:
            - payload: Dict[str, Any]
                錄製的欄位內容
            - type_name: str
                對應的 shioaji 訊息類別名，用來決定各欄位的型別
        """

        # 底線開頭：`__getattr__` 只在一般查找失敗時才被呼叫，
        # 這兩個屬性要走正常查找，否則會無限遞迴
        object.__setattr__(self, "_payload", payload)
        object.__setattr__(self, "_types", RECORDED_FIELD_TYPES.get(type_name, {}))

    def __getattr__(self, name: str) -> Any:
        payload: Dict[str, Any] = object.__getattribute__(self, "_payload")
        if name not in payload:
            raise AttributeError(name)

        types: Dict[str, str] = object.__getattribute__(self, "_types")
        return _restore(payload[name], types.get(name, ""))


def _restore(value: Any, type_name: str) -> Any:
    """依標註把 JSON 值還原成原本的型別；不認得的型別原樣回傳"""

    if value is None:
        return None

    if type_name == "Decimal":
        return Decimal(str(value))

    if type_name == "datetime":
        return (
            value
            if isinstance(value, datetime.datetime)
            else datetime.datetime.fromisoformat(str(value))
        )

    if type_name == "List[Decimal]" and isinstance(value, list):
        return [Decimal(str(item)) for item in value]

    return value


def load_recorded_messages(
    path: Path, kinds: Optional[Tuple[str, ...]] = ("tick_stk",)
) -> List[RecordedMessage]:
    """
    - Description:
        讀取錄製檔，還原成與券商推播同形狀的訊息

        **空的 `message` 一律略過並警告**：第一版錄製腳本因為取法不對，
        447 筆全錄成 `{}`。靜靜略過會讓重放看起來「沒有資料」，
        而真正的問題是那份錄製本身是廢的。
    - Parameters:
        - path: Path
            錄製的 JSONL
        - kinds: Optional[Tuple[str, ...]]
            只保留這些類型；None 表示全收
    - Return:
        - List[RecordedMessage]
            依錄製順序排列的訊息
    """

    messages: List[RecordedMessage] = []
    empty: int = 0

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue

            row: Dict[str, Any] = json.loads(line)
            kind: str = str(row.get("kind", ""))
            if kinds is not None and kind not in kinds:
                continue

            payload: Any = row.get("message")
            if not isinstance(payload, dict) or not payload:
                empty += 1
                continue

            messages.append(RecordedMessage(payload, KIND_TO_TYPE.get(kind, "")))

    if empty:
        logger.warning(
            f"{path} 有 {empty} 筆訊息是空的，已略過；"
            "那份錄製可能是用取不到欄位的舊版腳本產生的，建議重錄"
        )
    return messages


def replay_quotes(path: Path, stream: Any) -> List[BaseQuote]:
    """
    - Description:
        把錄製檔重放成報價清單

        走的是 `ShioajiQuoteStream.from_tick_message()`——**與實盤同一份轉換**，
        所以試撮與盤中零股同樣會被濾掉（它們回 `None`）。
    - Parameters:
        - path: Path
            錄製的 JSONL
        - stream: Any
            `ShioajiQuoteStream`；只用它的轉換方法
    - Return:
        - List[BaseQuote]
            轉換後的報價，依錄製順序
    """

    quotes: List[BaseQuote] = []
    for message in load_recorded_messages(path):
        quote: Optional[BaseQuote] = stream.from_tick_message(message)
        if quote is not None:
            quotes.append(quote)
    return quotes
