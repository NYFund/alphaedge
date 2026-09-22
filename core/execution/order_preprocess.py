from typing import Dict, List, Optional, Set

from loguru import logger

from core.models import BaseOrder
from core.utils import Action, BarExecutionOrder, PositionType

"""
委託前處理：方向白名單、單根 bar 的執行順序、持倉檔數上限、決定性排序

**這些都是「和撮合無關」的純邏輯**，回測與實盤共用同一份。實盤自己再寫一份的話，
兩邊會慢慢漂移，而漂移不會有任何錯誤訊息——只會讓實盤少送或多送一張單，
回測績效仍然漂亮。

函式一律只收純參數，不收策略物件也不收引擎，這樣它們才能被任何一邊呼叫。
事件計數由呼叫端傳入 `event_counts`，**key 不可更名**（報表相容）。
"""


def get_allowed_directions(
    allowed_directions: Optional[Set[PositionType]],
    position_type: PositionType,
) -> Set[PositionType]:
    """
    - Description:
        取得允許的訂單方向白名單；未指定時等同策略宣告的方向
    - Parameters:
        - allowed_directions: Optional[Set[PositionType]]
            策略宣告的白名單
        - position_type: PositionType
            策略的方向
    - Return:
        - Set[PositionType]
            允許的方向
    """

    return allowed_directions or {position_type}


def get_execution_order(
    bar_execution_order: Optional[BarExecutionOrder],
    position_type: PositionType,
    enable_intraday: bool,
) -> BarExecutionOrder:
    """
    - Description:
        決定單根 K 棒內的執行順序；策略顯式指定時一律以策略為準

        推導表（僅在 `bar_execution_order` 為 None 時適用）：

        | position_type | enable_intraday | 預設順序          |
        |---------------|-----------------|-------------------|
        | LONG          | 任意            | `CLOSE_THEN_OPEN` |
        | SHORT         | True            | `OPEN_THEN_CLOSE` |
        | SHORT         | False           | `CLOSE_THEN_OPEN` |

        SHORT ＋ 當沖採先開後平：現股當沖沖賣必須先賣才可能同日回補；
        留倉放空等同日頻再平衡，維持先平後開。

        **推導出的是預設建議，不是政策**：策略只要填了 `bar_execution_order`，
        這張表就完全不參與判斷。

        **LONG 為何不自動切換**：`enable_intraday` 的預設值是 True，
        既有做多策略沒有一支是刻意宣告當沖的。若讓 LONG ＋ `enable_intraday`
        自動採 `OPEN_THEN_CLOSE`，等於在無人宣告的情況下改掉每一支做多策略的
        成交順序與回測結果。做多當沖請在策略 `__init__` 顯式宣告。
    - Parameters:
        - bar_execution_order: Optional[BarExecutionOrder]
            策略顯式指定的順序
        - position_type: PositionType
            策略的方向
        - enable_intraday: bool
            是否允許當日沖銷
    - Return:
        - BarExecutionOrder
            單根 bar 的開平倉先後
    """

    if bar_execution_order is not None:
        return bar_execution_order

    if position_type == PositionType.SHORT and enable_intraday:
        return BarExecutionOrder.OPEN_THEN_CLOSE

    return BarExecutionOrder.CLOSE_THEN_OPEN


def resolve_open_action(position_type: PositionType) -> Action:
    """開倉動作：LONG 為買進、SHORT 為賣出（依訂單方向，不看策略）"""

    return Action.BUY if position_type == PositionType.LONG else Action.SELL


def resolve_close_action(position_type: PositionType) -> Action:
    """平倉動作：LONG 為賣出、SHORT 為買進回補"""

    return Action.SELL if position_type == PositionType.LONG else Action.BUY


def validate_orders(
    orders: List[BaseOrder],
    stage: str,
    allowed: Set[PositionType],
    event_counts: Optional[Dict[str, int]] = None,
) -> List[BaseOrder]:
    """
    - Description:
        檢查訂單方向是否合法，不合法者剔除並記錄，**禁止靜默丟棄**

        靜默丟棄的後果是策略以為自己送出了一張單、引擎以為沒有，
        兩邊都不會報錯，只有部位對不上。
    - Parameters:
        - orders: List[BaseOrder]
            策略回傳的訂單
        - stage: str
            `"open"` 或 `"close"`，決定期望的動作
        - allowed: Set[PositionType]
            允許的方向白名單
        - event_counts: Optional[Dict[str, int]]
            事件計數器；key 為 `rejected_direction`（**不可更名**，報表相容）
    - Return:
        - List[BaseOrder]
            通過檢查的訂單
    """

    valid_orders: List[BaseOrder] = []

    for order in orders:
        if order.position_type not in allowed:
            logger.warning(
                f"[Validate Order] {order.symbol} 方向 {order.position_type} "
                f"不在策略允許的 {allowed} 內，已剔除"
            )
            _count(event_counts, "rejected_direction")
            continue

        expected_action: Action = (
            resolve_open_action(order.position_type)
            if stage == "open"
            else resolve_close_action(order.position_type)
        )
        if order.action != expected_action:
            logger.warning(
                f"[Validate Order] {order.symbol} {stage} 動作應為 {expected_action}，"
                f"實際為 {order.action}，已剔除"
            )
            _count(event_counts, "rejected_direction")
            continue

        valid_orders.append(order)

    return valid_orders


def check_max_holdings(
    order: BaseOrder,
    max_holdings: Optional[int],
    held_symbols: Set[str],
    event_counts: Optional[Dict[str, int]] = None,
) -> bool:
    """
    - Description:
        引擎側的持倉檔數硬上限

        `max_holdings` 原本只是「策略願意遵守才生效」的建議值——引擎讀進來
        卻從未使用，實際上限落在每支策略自己算張數的那段程式裡。
        一支新策略只要不呼叫 sizer 就能無限開倉，且不會有任何警告。

        **與 `EqualWeightSizer.size()` 的同名檢查刻意不合併**：那邊是訊號階段
        「資金要切成幾份」，張數不足 1 張的候選不佔名額；這邊是逐單階段的硬上限，
        看**即時**持倉數——未成交的單不增加持倉，後面的單因此仍可能被放行。
        兩者不等價，少任何一道都會漏掉對方擋得住的情況。

        **已佔名額的標的一律放行**：那是加碼，不增加檔數，與
        `get_position_count()`「同一檔加碼多次只算一檔」的語意一致。
        這條豁免寫在這裡、回測與實盤共用：以前只有實盤豁免，滿額時的加碼單
        回測剔除、實盤送出，parity 比對每天都會多一筆 `UNEXPLAINED`。
    - Parameters:
        - order: BaseOrder
            待執行的開倉單
        - max_holdings: Optional[int]
            持倉檔數上限；None 表示不限制（與 `EqualWeightSizer` 的語意一致）
        - held_symbols: Set[str]
            目前佔住名額的標的（回測是未平倉部位；實盤另含尚未終結的委託）
        - event_counts: Optional[Dict[str, int]]
            事件計數器；key 為 `rejected_max_holdings`（**不可更名**）
    - Return:
        - bool
            True 表示可以開倉
    """

    if max_holdings is None:
        return True

    if order.symbol in held_symbols or len(held_symbols) < max_holdings:
        return True

    logger.warning(
        f"[Max Holdings] {order.symbol} 開倉單超過持倉檔數上限 {max_holdings}，已剔除"
    )
    _count(event_counts, "rejected_max_holdings")
    return False


def sort_orders(orders: List[BaseOrder]) -> List[BaseOrder]:
    """
    - Description:
        同一根 bar 內委託的決定性排序：依 `(date, symbol)` 做**穩定**排序

        為什麼要自己排：`check_max_holdings` 的截斷與部位管理層的餘額不足檢查，
        都會讓「先處理誰」直接改變成交結果。而委託的到達順序完全繼承自報價順序，
        報價又來自 `SELECT * FROM price WHERE date = ?`——這句沒有 `ORDER BY`，
        實際列順序取決於 SQLite 當下選到哪個索引。今天恰好等同依代號排序，
        但那是查詢計畫的副產物：多加一個索引、換一次 schema 就可能翻掉，
        且翻掉時不會報錯，只會讓回測結果無聲改變。

        實盤這邊的理由更直接：**它同時決定了跨策略搶同一標的時誰先拿到**，
        以及批次曝險超額時截斷的是哪幾張。

        **穩定排序**：同一標的的多筆委託維持策略給定的先後，
        分批建倉與部分平倉的意圖不會被打散。

        **已知限制**：Tick 級別的 `order.date` 只到「日」，因此同一 bar 內的
        tick 委託無法依成交時間排序，會被壓成依代號排序。
    - Parameters:
        - orders: List[BaseOrder]
            同一根 bar 內、同一個階段（開倉或平倉）的委託
    - Return:
        - List[BaseOrder]
            依穩定排序鍵重排後的委託
    """

    return sorted(orders, key=lambda order: (order.date, order.symbol))


def _count(event_counts: Optional[Dict[str, int]], key: str) -> None:
    """累加事件計數；未提供計數器時什麼都不做"""

    if event_counts is not None:
        event_counts[key] = event_counts.get(key, 0) + 1
