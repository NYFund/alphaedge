from abc import ABC, abstractmethod
from typing import List, Optional, Tuple

from core.models import BaseAccount, BaseQuote
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
                幾份」，張數不足 1 張的候選不佔名額；`order_preprocess.check_max_holdings()`
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
    LONG 回歸 baseline 的 915 筆結果。
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

        return sized
