from typing import Optional

from core.models import BaseQuote
from core.utils import Action, PositionType

"""
Signal: Alpha 層的輸出型別（選了哪個標的、什麼方向、用什麼價）

**報價不是訊號**：報價沒有方向、沒有強度，也分不出開倉與平倉，直接把
`List[BaseQuote]` 交給 portfolio 層的話，層次邊界表達不出來。

責任邊界（`sizing.py` 的模組說明是同一條線的另一半）：
- **策略決定**：選哪些標的、方向、下單價，以及**平倉要平多少**
- **Portfolio 層決定**：開倉各買幾張／幾口

`volume is None` 就是這條界線的具體化：None 表示「數量不歸策略管，交給
portfolio 層算」，那是開倉；填了值表示數量來自持倉查詢，那是平倉或停損。
"""


class Signal:
    """
    Alpha 層的訊號

    **價格有兩個欄位，不可合併**：
    - `order_price` 是要送出去的委託價，每個訊號都有。
    - `sizing_price` 只給開倉算量用，期貨（走保證金）與平倉（數量取自持倉）都是 None。

    以 `MomentumStrategy1` 的開倉為例，算量用 `quote.close`、下單價用 `quote.cur_price`。
    兩者在現行資料源恰好同值（`Scale.DAY` 與 `Scale.TICK` 都讓 `cur_price` 等於
    `close`），但那是**資料源的實作巧合，不是型別契約**——`BaseQuote` 的
    `cur_price` 與 `close` 是兩個獨立欄位。合併成一欄的話，哪天資料源讓兩者分家，
    錯的會是部位大小，而回歸不會有任何一筆交易變動來提醒你。

    **`sizing_price` 刻意不叫 `reference_price`**：`BaseOrder.reference_price` 已經
    被用來表示「滑價前的委託價」，是完全不同的東西。兩層之間同名不同義，遲早有人
    把其中一個傳給另一個。
    """

    def __init__(
        self,
        quote: BaseQuote,
        action: Action,
        position_type: PositionType,
        order_price: float,
        sizing_price: Optional[float] = None,
        volume: Optional[int] = None,
        strength: Optional[float] = None,
    ) -> None:
        # 產生本訊號的報價；portfolio 層要靠它取得計價單位與商品資訊
        self.quote: BaseQuote = quote

        # 方向資訊
        self.action: Action = action
        self.position_type: PositionType = position_type

        # 委託價（一定有）
        self.order_price: float = order_price

        # 算量用的參考價；期貨與平倉為 None（見 class docstring）
        self.sizing_price: Optional[float] = sizing_price

        # 數量：None 表示由 portfolio 層決定（開倉），有值表示取自持倉（平倉／停損）
        self.volume: Optional[int] = volume

        # 訊號強度：**目前沒有任何策略使用**，一律 None。
        #
        # 刻意保留，因為它是「換配置演算法時要不要動 Alpha 層」的分界：改成
        # 波動度加權或 conviction 加權時，portfolio 層需要一個強度輸入；沒有這個
        # 欄位的話，換演算法就得回頭改每一支策略的訊號產生邏輯。
        self.strength: Optional[float] = strength

    @property
    def symbol(self) -> str:
        """商品代號；直接取自報價，不另存一份以免兩者漂移"""

        return self.quote.symbol

    def __repr__(self) -> str:
        """除錯用：回歸比對失敗時要一眼看出是哪個標的、哪個方向、幾張"""

        return (
            f"Signal(symbol={self.symbol!r}, action={self.action}, "
            f"position_type={self.position_type}, order_price={self.order_price}, "
            f"sizing_price={self.sizing_price}, volume={self.volume})"
        )
