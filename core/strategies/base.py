import datetime
from abc import ABC, abstractmethod
from typing import List, Optional, Set

from core.models import BaseAccount, BaseOrder, BaseQuote
from core.portfolio.construction import BasePortfolioConstructor
from core.portfolio.signal import Signal
from core.utils import BarExecutionOrder, InstrumentType, Market, PositionType, Scale

"""BaseStrategy: 市場與商品皆無關的策略骨架（market ＋ instrument_type 為 factory 的分派鍵）"""


class BaseStrategy(ABC):
    """Strategy Framework (Market/Instrument-agnostic Base Template)"""

    def __init__(self) -> None:
        """=== Account Setting ==="""
        self.account: Optional[BaseAccount] = None  # 虛擬帳戶資訊

        """ === Strategy Setting === """
        self.strategy_name: str = ""  # Strategy name
        # 市場（地區）與商品類別是兩條正交的軸，兩者的**組合**才是 factory 的分派鍵：
        # model 組合本來就是按組合實作的（`TwStockSpec` ＝ TW ＋ STOCK）。
        # 兩者皆由各市場的策略基底填入，策略本身不需設定。
        self.market: Optional[Market] = None  # 市場（地區）
        self.instrument_type: Optional[InstrumentType] = None  # 商品類別
        # 策略主要方向（推導預設值用）
        self.position_type: PositionType = PositionType.LONG
        # 策略是否為當沖：只是**推導預設值的輸入**，不是硬性開關。
        # 真正決定同一根 bar 能否開平同一標的的是 bar_execution_order（見下方區塊）
        self.enable_intraday: bool = True  # Allow day trade or not
        self.init_capital: float = 0  # Initial capital
        # 同時可持有的最大檔數；**預設 None ＝ 不限制**。
        #
        # 舊版預設 0，而 `Backtester.check_max_holdings()` 只把 None 當成不限制
        # ——於是**忘記設定的新策略，每一張開倉單都被引擎剔除**，回測跑完是
        # 零筆交易、零錯誤訊息。寧可預設不限制（策略自己的 sizer 仍會把關），
        # 也不要用一個看起來像「還沒設定」的值去無聲地擋掉所有交易。
        self.max_holdings: Optional[int] = None

        """
        === Direction Setting ===

        方向的責任分工：
        - position_type 只用來推導預設值，不參與記帳
        - allowed_directions 是訂單方向的白名單，None 時等同 {position_type}
        - 實際記帳與成本路徑一律看每一張 order 的 position_type

        方向（LONG／SHORT）與商品類別（股票／期貨）是兩條獨立的軸，故本區塊與商品無關。

        執行順序的推導（`Backtester.get_execution_order()`，完整對照表在該處）：

        | position_type | enable_intraday | 推導出的預設 bar_execution_order |
        |---------------|-----------------|----------------------------------|
        | LONG          | 任意            | `CLOSE_THEN_OPEN`                |
        | SHORT         | True            | `OPEN_THEN_CLOSE`                |
        | SHORT         | False           | `CLOSE_THEN_OPEN`                |

        **推導出的只是預設建議，一旦策略在 `__init__` 填了 `bar_execution_order`，
        引擎一律以策略宣告為準。** 做多當沖要在同一根 bar 內開平同一標的，
        必須自己宣告 `OPEN_THEN_CLOSE`——`enable_intraday=True` 不會自動切換，
        因為它的預設值就是 True，既有做多策略沒有一支是刻意宣告當沖的，
        自動切換等於在無人宣告的情況下改掉所有做多策略的回測結果。
        """
        self.allowed_directions: Optional[Set[PositionType]] = None  # 允許的訂單方向
        self.bar_execution_order: Optional[BarExecutionOrder] = (
            None  # 單根 K 棒內的執行順序（None 由引擎推導，見上表；非 None 時一律以策略為準）
        )

        """ === Backtest Setting === """
        self.scale: str = Scale.DAY  # Backtest scale: DAY / TICK
        self.start_date: datetime.date = None  # Optional: 回測起始日
        self.end_date: datetime.date = None  # Optional: 回測結束日

    @abstractmethod
    def setup_account(self, account: BaseAccount) -> None:
        """
        - Description:
            載入虛擬帳戶資訊
        """
        pass

    # === Alpha 層：策略只需要實作這些 ===
    def generate_open_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """
        - Description:
            開倉訊號：選標的、定方向、給價，**不決定數量**

            數量由 portfolio 層依資金或保證金換算（見 `check_open_signal()`），
            故回傳的 `Signal` 一律不填 `volume`。

            尚未搬到分層鉤子的策略仍自行覆寫 `check_open_signal()`，那條路徑
            不會走到這裡；**兩條路徑只能擇一**，同時覆寫等於讓本方法變成死碼。
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[Signal]
                開倉訊號
        """

        raise NotImplementedError(
            f"{type(self).__name__} 尚未實作 generate_open_signals()"
        )

    def generate_close_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """
        - Description:
            平倉訊號：挑哪些部位出場、用什麼價，**數量也由策略決定**

            與開倉相反，平倉的數量**不經過 portfolio 層**：它來自持倉查詢，
            而「平掉第一筆部位」與「合併同標的所有部位」是策略決策
            （後者若逐筆送單，會被 `close_position()` 的 FIFO 吃掉）。
            故回傳的 `Signal` 必須填 `volume` 與 `order_price`。
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[Signal]
                平倉訊號
        """

        raise NotImplementedError(
            f"{type(self).__name__} 尚未實作 generate_close_signals()"
        )

    def generate_stop_loss_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """
        - Description:
            停損訊號；語意與 `generate_close_signals()` 相同，只是觸發條件不同
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[Signal]
                停損（平倉）訊號
        """

        raise NotImplementedError(
            f"{type(self).__name__} 尚未實作 generate_stop_loss_signals()"
        )

    def build_close_orders(self, signals: List[Signal]) -> List[BaseOrder]:
        """
        - Description:
            把平倉／停損訊號組成訂單

            **只做欄位搬運**，不做任何數量或價格決策——兩者都已由策略在
            `Signal` 裡給定。由各市場基底實作（組出 `StockOrder`／`FuturesOrder`）。
        - Parameter:
            - signals: List[Signal]
                已填好 `volume` 與 `order_price` 的平倉訊號
        - Return:
            - List[BaseOrder]
                平倉訂單；`volume` 未填或不大於 0 者略過
        """

        raise NotImplementedError(f"{type(self).__name__} 沒有可用的平倉組裝")

    def make_portfolio_constructor(self) -> BasePortfolioConstructor:
        """
        - Description:
            建立本次組裝要用的部位建構器

            **刻意不是 `@abstractmethod`**：部位建構器是市場專屬的，由
            `BaseStockStrategy`／`BaseFuturesStrategy` 提供，策略本身不需要宣告。
            在這一層掛 abstract 會讓所有直接繼承 `BaseStrategy` 的類別（含測試
            的 stub）變成抽象類別，而 `StrategyLoader` 會**靜默跳過**抽象類別
            ——那是「加一個抽象方法就讓既有子類從清單裡消失」的無聲故障。

            **每次組裝都重建，不快取**：策略的 `max_holdings`／`max_lots` 在
            `__init__` 呼叫 `super().__init__()` 之後才填，`margin_config` 更是由
            `core/backtest/factory.py` 在策略建構完成後才注入。建一次存起來會
            永遠看到舊值，而症狀是部位大小整段偏掉，不會有任何錯誤訊息。

            要換配置演算法（波動度加權等）時覆寫本方法即可。
        - Return:
            - BasePortfolioConstructor
                對應本市場的部位建構器
        """

        raise NotImplementedError(f"{type(self).__name__} 沒有可用的部位建構器")

    # === 引擎契約：由基底提供，策略不需要實作 ===
    def check_open_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        """
        - Description:
            開倉：Alpha 選出候選後交由 portfolio 層換算部位
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[BaseOrder]
                開倉訂單
        """

        if self.account is None:
            return []

        signals: List[Signal] = self.generate_open_signals(quotes)
        if not signals:
            return []

        return self.make_portfolio_constructor().build(signals, self.account)

    def check_close_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        """
        - Description:
            平倉：數量與價格都由策略在訊號裡給定，基底只負責組單

            **不經過 portfolio 層**，理由見 `generate_close_signals()`。
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[BaseOrder]
                平倉訂單
        """

        if self.account is None:
            return []

        return self.build_close_orders(self.generate_close_signals(quotes))

    def check_stop_loss_signal(self, quotes: List[BaseQuote]) -> List[BaseOrder]:
        """
        - Description:
            停損：與平倉走同一條組裝路徑，只是訊號來源不同
        - Parameter:
            - quotes: List[BaseQuote]
                目標商品的報價資訊
        - Return:
            - List[BaseOrder]
                停損（平倉）訂單
        """

        if self.account is None:
            return []

        return self.build_close_orders(self.generate_stop_loss_signals(quotes))
