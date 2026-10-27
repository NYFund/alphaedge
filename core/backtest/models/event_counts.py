from typing import Dict, Tuple

"""
事件計數器的 key 清單：回測期間被拒、被縮量、被強制出場的訂單各計一筆

**放在 `core/backtest/models/` 而不是引擎**：`FillModel` 與 `SettlementModel`
在沒有引擎的情況下（單獨測試、或被別的組裝方式使用）也要能自備一份計數器，
而它們在分層上低於引擎——由它們 import 引擎會變成反向相依。

**key 分「共用」與「各市場」**：共用的由引擎、委託規則、成交模型基底、部位管理層與
兩個市場的結算模型計入；台股專屬的是信用交易、當沖、除權息那一組，期貨專屬的是換月轉倉。
各市場的事件報表只列自己用得到的 key——期貨報表列二十個永遠是 0 的台股列，
讀的人分不出「沒發生」與「不可能發生」。

既有 key 與報表欄位相容，**不可更名**（新增可以）：重新命名會讓歷史
`*_event_report.csv` 對不上。台股清單的**順序**同樣不可動：事件報表與 SHORT 回歸基準
都以它為欄位順序。
"""

# key 的適用範圍
SHARED: str = (
    "shared"  # 引擎、委託規則、成交模型基底、部位管理層、兩個市場的結算模型都會計入
)
STOCK: str = "stock"  # 只有台股會計入

# 台股事件報表的完整清單（含共用）；順序即報表順序，**不可動**
_STOCK_EVENT_KEY_SCOPES: Tuple[Tuple[str, str], ...] = (
    ("rejected_direction", SHARED),  # 方向不合法被剔除的訂單
    ("rejected_fill_price", SHARED),  # 成交價不合理被拒的訂單
    ("fill_price_clamped", SHARED),  # 滑價把成交價推出當日區間、被夾回的開倉單
    ("close_price_out_of_range", SHARED),  # 平倉成交價超出當日區間（不拒單，只計數）
    ("forced_cover_day_trade", STOCK),  # 當沖日終強制回補
    ("forced_cover_margin_call", SHARED),  # 維持率追繳強制回補
    ("forced_cover_insufficient_margin", STOCK),  # 當沖轉留倉時餘額不足而強制回補
    ("forced_cover_max_holding", STOCK),  # 超過最長持有天數強制回補
    ("forced_cover_suspended", STOCK),  # 停券日強制回補
    (
        "forced_cover_no_quote",
        SHARED,
    ),  # 連續無報價（停牌／下市／契約到期）強制回補的放空部位
    ("limit_up_cover_failed", STOCK),  # 漲停鎖死無法回補
    ("rejected_max_holdings", SHARED),  # 超過最大持倉檔數被引擎剔除的開倉單
    ("rejected_no_quote", SHARED),  # 當日查不到報價（停牌、非股票池）被拒的開倉單
    (
        "rejected_insufficient_balance",
        SHARED,
    ),  # 可動用餘額不足以支應開倉：台股做多為部位價值 ＋ 開倉成本、期貨為保證金 ＋ 交易成本
    ("rejected_no_borrow", STOCK),  # 融券餘額不足被拒的放空開倉單
    ("rejected_short_suspended", STOCK),  # 停券期間被拒的融券放空開倉單
    ("rejected_short_halted", STOCK),  # 名單外或被註記暫停融券／借券，被拒的放空開倉單
    ("rejected_below_reference", STOCK),  # 平盤下不得融（借）券賣出，被拒的放空開倉單
    (
        "rejected_not_day_tradable",
        STOCK,
    ),  # 不在現股當沖名單或暫停先賣後買，被拒的當沖放空單
    ("rejected_limit_up_locked", STOCK),  # 全日鎖漲停、買不到，被拒的買進開倉單
    ("rejected_limit_down_locked", STOCK),  # 全日鎖跌停、賣不掉，被拒的放空開倉單
    ("rejected_volume_cap", SHARED),  # 超過當日成交量上限被拒的訂單
    ("truncated_by_volume", SHARED),  # 超過當日成交量上限被縮量的訂單
    ("dividend_compensation_paid", STOCK),  # 除息日補償出借方股利的空單
    ("dividend_compensation_unknown", STOCK),  # 因權息並存無法拆分股利而跳過補償的空單
    ("dividend_received", STOCK),  # 除息日收到現金股利的做多部位
    ("share_adjustment_applied", STOCK),  # 配股、分割、減資造成股數調整的部位
    ("share_adjustment_unknown", STOCK),  # 權息並存拆不出配股率而未調整股數的部位
    (
        "forced_exit_no_quote",
        SHARED,
    ),  # 連續無報價（停牌／下市／契約到期）強制出場的做多部位
    # 新 key 一律加在尾端：事件報表與 SHORT 回歸基準以這份順序為欄位
    ("rejected_short_exposure", STOCK),  # 放空曝險超過單一標的上限被拒的開倉單
)

# 台股事件報表的 key（原順序）；名稱維持 `EVENT_KEYS`，既有呼叫端與測試都以它為台股清單
EVENT_KEYS: Tuple[str, ...] = tuple(key for key, _ in _STOCK_EVENT_KEY_SCOPES)

# 兩個市場共用的 key（維持台股清單內的相對順序）
SHARED_EVENT_KEYS: Tuple[str, ...] = tuple(
    key for key, scope in _STOCK_EVENT_KEY_SCOPES if scope == SHARED
)

# 只有期貨會計入的 key
FUTURES_ONLY_EVENT_KEYS: Tuple[str, ...] = (
    "rolled_contract",  # 換月轉倉（舊契約平倉、新契約開倉）的部位
)

# 期貨事件報表的 key：共用的在前、期貨專屬的在後
FUTURES_EVENT_KEYS: Tuple[str, ...] = SHARED_EVENT_KEYS + FUTURES_ONLY_EVENT_KEYS

# 只有逐筆回測才有的事件：由 `TickSimulator` 建立時加進共用的計數器。
# **不放進任何市場的清單**：日 K 回測的事件報表與 SHORT 回歸基準以市場清單為欄位，
# 多三個永遠是 0 的欄位只會讓既有基準失效，換不到任何資訊
TICK_EVENT_KEYS: Tuple[str, ...] = (
    "expired_ioc",  # 市價（IOC）委託在下一筆無法成交而作廢
    "expired_at_close",  # 限價（ROD）委託到收盤仍未成交而作廢
    "rejected_duplicate_pending",  # 同一檔已有在途開倉委託，新的開倉單被略過
)


def new_event_counts(keys: Tuple[str, ...] = EVENT_KEYS) -> Dict[str, int]:
    """
    - Description:
        建立事件計數器；由 factory 依市場建立後交給引擎、FillModel 與 SettlementModel
        共用同一個 dict

        預設為台股清單：既有的呼叫端與測試大多是台股，不帶參數時行為不變。
    - Parameters:
        - keys: Tuple[str, ...]
            這個市場會用到的 key（`EVENT_KEYS` 或 `FUTURES_EVENT_KEYS`）
    - Return:
        - Dict[str, int]
            每個 key 都為 0 的計數器，順序即事件報表的順序
    """

    return dict.fromkeys(keys, 0)
