import datetime
from typing import Any, Dict, List, Optional, Sequence

import pandas as pd
from loguru import logger

from core.models import BaseQuote

"""
報價驗證：來源無關的純函式

三條規則原本只長在 `StockQuoteAdapter` 上，而**另外兩條路徑一條都沒有**：
實盤的券商快照會把缺值變成 0 元報價，期貨回測完全沒有重複代號偵測。
同一個坑在不同來源上會再踩一次，而且兩次的症狀都是「沒有錯誤訊息」。

**簽章吃值不吃列**（`close: Optional[float]` 而不是 `row: Any`）：
這樣它與來源的欄位名完全無關，`price` 表、Shioaji `Snapshot`、
`futures_price` 表都能套同一份。

**寫成模組層級純函式**（比照 `core/live/risk/` 的金額與曝險規則）：
連實例都不必建就測得動，而報價驗證正是最該有密集邊界測試的東西。
"""


def has_valid_price(close: Any) -> bool:
    """
    - Description:
        這個收盤價可不可以拿來成交

        **無成交日的 OHLC 是 NULL（或歷史資料裡的 0）**：來源給的是 `--`，
        舊版 cleaner 填成 0 之後就變成「當天成交價是 0 元」，回測會照著它成交。
        cleaner 已改為保留 NULL，這裡把兩種形態一起濾掉，
        讓尚未執行修復腳本的資料庫也不會拿 0 元價去成交。

        **負數同樣擋掉**：它不是任何一種合法的股價或期貨價，
        出現就代表上游解析錯了。
    - Parameters:
        - close: Any
            收盤價；`None`、`NaN`、非數值皆視為無效
    - Return:
        - bool
            可成交為 True
    """

    if close is None:
        return False

    try:
        if pd.isna(close):
            return False
        return float(close) > 0
    except (TypeError, ValueError):
        # 非數值（字串、物件）一律視為無效；讓它往上拋只會在轉換途中炸掉整天的報價
        return False


def find_duplicate_symbols(quotes: Sequence[BaseQuote]) -> List[str]:
    """
    - Description:
        找出同一根 bar 內出現多次的代號（已排序）
    - Parameters:
        - quotes: Sequence[BaseQuote]
            當根 bar 的報價
    - Return:
        - List[str]
            重複的代號；沒有重複時為空 list
    """

    seen: Dict[str, int] = {}
    for quote in quotes:
        seen[quote.symbol] = seen.get(quote.symbol, 0) + 1

    return sorted(symbol for symbol, count in seen.items() if count > 1)


def warn_duplicate_symbols(
    quotes: Sequence[BaseQuote],
    date: datetime.date,
    source: str = "Quote",
) -> List[str]:
    """
    - Description:
        同一根 bar 內出現重複代號時發出警告

        重複代表資料層無法唯一識別商品——例如上市股與上櫃 ETF 共用同一個
        4 碼代號；期貨則可能是日盤／夜盤同契約、合併時段失敗、
        或週契約與月契約代號碰撞。引擎後續會以 `{q.symbol: q for q in quotes}`
        建對照表，重複的只會留下最後一筆，**成交價與訊號都可能取到另一檔商品**，
        而且整個過程不會有任何錯誤。

        **只警告不排除**：要留哪一筆屬資料修正的範疇，靜默挑一筆才是更糟的選擇。
    - Parameters:
        - quotes: Sequence[BaseQuote]
            當根 bar 的報價
        - date: datetime.date
            當前交易日（僅供訊息辨識）
        - source: str
            來源標籤，讓 log 看得出是哪一條路徑
    - Return:
        - List[str]
            重複的代號（同時回傳，讓呼叫端可據以決定要不要另外處理）
    """

    duplicates: List[str] = find_duplicate_symbols(quotes)
    if duplicates:
        logger.warning(
            f"[{source}] {date} 有 {len(duplicates)} 個代號對應多筆報價："
            f"{duplicates[:10]}；建對照表時只會留下最後一筆，"
            f"請確認該代號是否被不同商品共用"
        )

    return duplicates


def resolve_close(raw: Any) -> Optional[float]:
    """
    - Description:
        把來源的收盤價轉成可用的浮點數；無效時回 `None`

        **回 `None` 而不是 0**：券商快照缺值時原本走
        `float(getattr(snapshot, "close", 0.0) or 0.0)`，於是停牌或快照不完整的
        標的會變成一個「0 元」的報價物件——回測會濾掉這檔，實盤卻照樣拿去算訊號
        與成交價。回 `None` 讓呼叫端明確略過。
    - Parameters:
        - raw: Any
            來源的收盤價
    - Return:
        - Optional[float]
            有效價格；無效時為 None
    """

    if not has_valid_price(raw):
        return None
    return float(raw)
