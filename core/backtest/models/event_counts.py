from typing import Dict, Tuple

"""
事件計數器的 key 清單：回測期間被拒、被縮量、被強制出場的訂單各計一筆

**放在 `core/backtest/models/` 而不是引擎**：`FillModel` 與 `SettlementModel`
在沒有引擎的情況下（單獨測試、或被別的組裝方式使用）也要能自備一份計數器，
而它們在分層上低於引擎——由它們 import 引擎會變成反向相依。

既有 key 與報表欄位相容，**不可更名**（新增可以）：重新命名會讓歷史
`*_event_report.csv` 對不上。
"""

EVENT_KEYS: Tuple[str, ...] = (
    "rejected_direction",  # 方向不合法被剔除的訂單
    "rejected_fill_price",  # 成交價不合理被拒的訂單
    "fill_price_clamped",  # 滑價把成交價推出當日區間、被夾回的開倉單
    "close_price_out_of_range",  # 平倉成交價超出當日區間（不拒單，只計數）
    "forced_cover_day_trade",  # 當沖日終強制回補
    "forced_cover_margin_call",  # 維持率追繳強制回補
    "forced_cover_insufficient_margin",  # 當沖轉留倉時餘額不足而強制回補
    "forced_cover_max_holding",  # 超過最長持有天數強制回補
    "forced_cover_suspended",  # 停券日強制回補
    "forced_cover_no_quote",  # 連續無報價（停牌／下市）強制出場
    "limit_up_cover_failed",  # 漲停鎖死無法回補
    "rejected_max_holdings",  # 超過最大持倉檔數被引擎剔除的開倉單
    "rejected_no_quote",  # 當日查不到報價（停牌、非股票池）被拒的開倉單
    "rejected_insufficient_balance",  # 餘額不足以支應做多開倉（部位價值 ＋ 開倉成本）
    "rejected_no_borrow",  # 融券餘額不足被拒的放空開倉單
    "rejected_short_suspended",  # 停券期間被拒的融券放空開倉單
    "rejected_limit_up_locked",  # 全日鎖漲停、買不到，被拒的買進開倉單
    "rejected_limit_down_locked",  # 全日鎖跌停、賣不掉，被拒的放空開倉單
    "rejected_volume_cap",  # 超過當日成交量上限被拒的訂單
    "truncated_by_volume",  # 超過當日成交量上限被縮量的訂單
    "dividend_compensation_paid",  # 除息日補償出借方股利的空單
    "dividend_compensation_unknown",  # 因權息並存無法拆分股利而跳過補償的空單
    "dividend_received",  # 除息日收到現金股利的做多部位
    "share_adjustment_applied",  # 配股、分割、減資造成股數調整的部位
    "share_adjustment_unknown",  # 權息並存拆不出配股率而未調整股數的部位
    "forced_exit_no_quote",  # 連續無報價（停牌／下市）強制出場的做多部位
)


def new_event_counts() -> Dict[str, int]:
    """建立事件計數器；由 factory 建立後交給引擎、FillModel 與 SettlementModel 共用同一個 dict"""

    return dict.fromkeys(EVENT_KEYS, 0)
