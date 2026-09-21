import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from shioaji import stream_data_type

from core.models import BaseQuote

"""
把錄製的行情重放回來

**重放一定要走真正的轉換路徑**（`ShioajiQuoteStream.to_tick_quote()`），
否則驗到的只是「JSON 讀得出來」，而不是「這份資料餵進系統會得到什麼」。
故本模組只負責把 JSON 還原成**與券商推播同形狀的物件**，轉換交給原本那一份。

**型別要照著還原**：JSON 沒有 `Decimal` 與 `datetime`，錄製時它們被序列化成字串。
直接把字串餵進轉換層，`float('4930')` 雖然會過，但 `datetime` 那一欄會當場炸——
而更糟的情況是某天有人加了 `.get()` 預設值，於是重放靜靜地產出錯的報價。
型別來源是**安裝的套件本身**（`stream_data_type` 的標註），不是寫死的清單。
"""

# 欄位 → 型別名。跟著安裝的 shioaji 走，升版時自動對齊
_FIELD_TYPES: Dict[str, Dict[str, str]] = {
    name: {
        field: getattr(annotation, "__name__", str(annotation))
        for field, annotation in (getattr(cls, "__annotations__", {}) or {}).items()
    }
    for name, cls in vars(stream_data_type).items()
    if isinstance(cls, type)
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

        **以 `__getattr__` 提供欄位而不是先塞進 `__dict__`**：真品是 C 擴充物件，
        沒有 `__dict__`、`dict()`、`model_dump()`，連 `dir()` 都是空的。
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
        object.__setattr__(self, "_types", _FIELD_TYPES.get(type_name, {}))

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

        走的是 `ShioajiQuoteStream.to_tick_quote()`——**與實盤同一份轉換**，
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
        quote: Optional[BaseQuote] = stream.to_tick_quote(message)
        if quote is not None:
            quotes.append(quote)
    return quotes
