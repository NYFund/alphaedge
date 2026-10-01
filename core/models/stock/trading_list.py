from dataclasses import dataclass, field
from typing import FrozenSet

"""
交易所每日公告的兩份資格名單，在回測裡的單日快照

由資料源依當日公告建立、交給成交模型判斷放空開倉能不能成交。
**快照不存在（`None`）與「名單是空的」是兩回事**：前者代表沒有資料，
後者代表當天真的沒有任何標的符合——資料源負責保證開啟檢核時每天都有快照。
"""


@dataclass(frozen=True)
class ShortSaleListSnapshot:
    """
    單日的平盤下得融（借）券賣出名單

    名單本身＝**當日可融資融券的全部證券**，三個註記再把其中一部分限縮。
    現股當沖的先賣後買是現股賣出，不受本名單限制。
    """

    listed: FrozenSet[str] = field(default_factory=frozenset)  # 可融資融券的證券
    margin_halted: FrozenSet[str] = field(default_factory=frozenset)  # 暫停融券賣出
    sbl_halted: FrozenSet[str] = field(default_factory=frozenset)  # 暫停借券賣出
    # 前一交易日收盤跌停，本日禁止平盤下融（借）券賣出
    below_reference_banned: FrozenSet[str] = field(default_factory=frozenset)

    def allows_margin_short(self, symbol: str) -> bool:
        """當日能否融券賣出（不論價位）：須在名單內且未暫停融券"""

        return symbol in self.listed and symbol not in self.margin_halted

    def allows_sbl_short(self, symbol: str) -> bool:
        """
        當日能否借券賣出（不論價位）

        **不要求在名單內**：借券賣出不以融資融券資格為前提，名單外的證券
        只是不得在平盤以下賣出（見 `allows_below_reference()`）。
        """

        return symbol not in self.sbl_halted

    def allows_below_reference(self, symbol: str) -> bool:
        """當日能否以低於平盤（參考價）的價格融（借）券賣出"""

        return symbol in self.listed and symbol not in self.below_reference_banned


@dataclass(frozen=True)
class DayTradeListSnapshot:
    """單日的現股當沖標的名單"""

    day_tradable: FrozenSet[str] = field(default_factory=frozenset)  # 可現股當沖
    sell_first_halted: FrozenSet[str] = field(default_factory=frozenset)  # 暫停先賣後買

    def allows_sell_first(self, symbol: str) -> bool:
        """當日能否以「先賣後買」做現股當沖（現股當沖放空）"""

        return symbol in self.day_tradable and symbol not in self.sell_first_halted
