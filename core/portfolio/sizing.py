from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from loguru import logger

from core.models import BaseAccount, BaseQuote, FuturesAccount
from core.utils import Units

"""
部位大小模型：把「檔數上限 ＋ 等權資金切分 ＋ 張數換算」收成單一實作

**公式只留一份**：讓各策略各寫一遍的話，`max_holdings is None` 這種邊界語意
會在策略之間悄悄分岔，而分岔不會報錯，只會讓部位大小整段偏掉。

責任邊界（新增策略時請遵守）：
- **策略決定**：選哪些標的、用什麼參考價、訂單的方向與動作
- **Sizer 決定**：這些候選各買幾張

參考價**由呼叫端傳入**：要用 `close`、`tick_quote.close` 還是 `open` 是策略決策，
不該由 sizer 代為選擇。
"""


class BasePositionSizer(ABC):
    """
    部位大小模型的共用介面

    日後要加波動度加權／風險平價，新增實作即可，呼叫端不動。
    """

    @abstractmethod
    def size(
        self,
        account: BaseAccount,
        candidates: List[Tuple[BaseQuote, float]],
        max_holdings: Optional[int],
    ) -> List[Tuple[BaseQuote, float, int]]:
        """
        - Description:
            決定每個候選標的要下幾張
        - Parameters:
            - account: BaseAccount
                虛擬帳戶，提供餘額與現有持倉檔數
            - candidates: List[Tuple[BaseQuote, float]]
                (報價, 該策略選定的參考價)
            - max_holdings: Optional[int]
                最大持倉檔數；`None` 表示不限制。

                **引擎側另有一道同名檢查，兩者刻意不合併**：這裡回答「資金要切成
                幾份」，張數不足 1 張的候選不佔名額；`order_rules.check_max_holdings()`
                回答「這張單送出去會不會讓帳戶超過上限」，看的是逐單執行當下的
                即時持倉數（未成交的單不增加持倉）。兩者不等價，少任何一道都會漏掉
                對方擋得住的情況
        - Return:
            - List[Tuple[BaseQuote, float, int]]
                (報價, 參考價, 張數)；張數不足 1 張者不回傳
        """

        pass


class EqualWeightSizer(BasePositionSizer):
    """
    等權資金切分：可開檔數均分餘額，逐檔換算張數

    ```
    可開檔數 = max(0, max_holdings - 現有持倉檔數)   # max_holdings 為 None 時不限制
    每檔資金 = account.balance / 可開檔數
    張數     = int(每檔資金 / (參考價 × Units.LOT))   # 無條件捨去
    下單條件 = 張數 >= 1
    每下一單，可開檔數 -= 1；歸零即停止
    ```

    **`int()` 的無條件捨去與「至少 1 張」的門檻不可改動**——它們直接決定
    LONG 回歸 baseline（VolumeBreakoutMomentumStrategy 2024 全年，1,861 筆）的逐筆結果。
    """

    def size(
        self,
        account: BaseAccount,
        candidates: List[Tuple[BaseQuote, float]],
        max_holdings: Optional[int],
    ) -> List[Tuple[BaseQuote, float, int]]:
        """依剩餘可開倉名額均分餘額並換算張數"""

        available_position_cnt: int = (
            max(0, max_holdings - account.get_position_count())
            if max_holdings is not None
            else len(candidates)
        )

        if available_position_cnt <= 0:
            if candidates:
                logger.info(
                    f"[Sizing] 持倉已達上限 {max_holdings} 檔，"
                    f"{len(candidates)} 個開倉候選全數略過"
                )
            return []

        per_position_size: float = account.balance / available_position_cnt

        sized: List[Tuple[BaseQuote, float, int]] = []
        for quote, ref_price in candidates:
            if available_position_cnt == 0:
                break

            # 參考價無效時跳過；少了這道檢查會直接 ZeroDivisionError 中斷整場回測
            if ref_price <= 0:
                continue

            open_volume: int = int(per_position_size / (ref_price * Units.LOT))

            if open_volume >= 1:
                sized.append((quote, ref_price, open_volume))
                available_position_cnt -= 1
            else:
                # 不記的話，整批訊號被切成 0 張時 log 只剩「沒有任何委託」，
                # 看不出是資金切太細還是訊號根本沒出來
                logger.info(
                    f"[Sizing] {quote.symbol} 每檔資金 {per_position_size:,.0f} "
                    f"不足一張（參考價 {ref_price} × {Units.LOT} ＝ "
                    f"{ref_price * Units.LOT:,.0f}），略過"
                )

        return sized


class MarginSizer(BasePositionSizer):
    """
    保證金約束的口數：可動用餘額 × `max_capital_usage` ÷ 每口需要的資金，再與剩餘口數上限取小值

    ```
    剩餘口數 = max_lots − 帳上已開口數（多空各算、不相抵）
    預算     = account.balance × max_capital_usage
    口數     = min(int(預算 / 每口需要的資金), 剩餘口數)   # 無條件捨去
    每下一單，剩餘口數 −= 口數；歸零即停止（預算不逐單扣減，與原本的演算法相同）
    ```

    候選的第二個元素是**每口需要的資金**（原始保證金 ＋ 開倉手續費與稅），由部位建構層
    查保證金模型算好傳入——與 `EqualWeightSizer` 的參考價同一個位置，sizer 本身不查表。
    `max_holdings` 不用：期貨限制的是總口數，不是持倉檔數。

    **拿契約價值去除餘額會嚴重低估**（TX 一口契約價值 900 萬、保證金只有 70 萬），
    那正是期貨不能沿用等權切分的理由。
    """

    def __init__(self, max_lots: int = 0, max_capital_usage: float = 0.5) -> None:
        self.max_lots: int = max_lots  # 總口數上限（0 表示不開倉）
        self.max_capital_usage: float = max_capital_usage  # 可動用餘額的使用上限

    def size(
        self,
        account: BaseAccount,
        candidates: List[Tuple[BaseQuote, float]],
        max_holdings: Optional[int],
    ) -> List[Tuple[BaseQuote, float, int]]:
        """依剩餘口數與每口需要的資金算口數；`max_holdings` 對期貨沒有意義，忽略"""

        open_lots: int = (
            sum(abs(lots) for lots in account.get_open_lots().values())
            if isinstance(account, FuturesAccount)
            else 0
        )
        remaining_lots: int = self.max_lots - open_lots
        budget: float = account.balance * self.max_capital_usage

        sized: List[Tuple[BaseQuote, float, int]] = []
        for quote, requirement in candidates:
            if remaining_lots <= 0:
                break
            # 每口需要的資金無效時跳過；少了這道檢查會直接 ZeroDivisionError 中斷整場回測
            if requirement <= 0:
                continue

            lots: int = min(int(budget // requirement), remaining_lots)
            if lots <= 0:
                logger.info(
                    f"[Sizing] {quote.symbol} 預算 {budget:,.0f} 不足一口"
                    f"（每口需要 {requirement:,.0f}），略過"
                )
                continue

            sized.append((quote, requirement, lots))
            remaining_lots -= lots

        return sized
