import datetime
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from core.models import BaseAccount, BaseOrder, BaseQuote
from core.portfolio.construction import BasePortfolioConstructor
from core.portfolio.signal import Signal
from core.utils import (
    ExecutionStyle,
    ExecutionTiming,
    InstrumentType,
    Market,
    Scale,
    TradeDirection,
)

"""BaseStrategy: 市場與商品皆無關的策略骨架（market ＋ instrument_type 為 factory 的分派鍵）"""

# 已移除的策略設定欄位 → 改用什麼。
# Python 允許策略在 `__init__` 指派任意屬性，照舊寫法設這些欄位不會報錯，
# 引擎卻完全不讀——策略作者以為設定生效，回測與實盤照預設值跑。
_REMOVED_SETTINGS: Dict[str, str] = {
    "position_type": "改用 `direction`（TradeDirection.LONG／SHORT／BOTH）",
    "allowed_directions": "改用 `direction`；多空都做設 TradeDirection.BOTH",
    "enable_intraday": "改用 `allow_day_trade`（預設 False）",
    "bar_execution_order": "已移除：執行順序一律由 `allow_day_trade` 推導",
    "is_intraday": "改用 `is_tick_triggered`",
}


class RemovedStrategySettingError(ValueError):
    """策略設定了已移除的欄位：引擎不會讀它，照跑只會靜默套用預設值"""


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
        # 是否為**盤中逐筆觸發**的策略：實盤每收到一筆 tick 就呼叫一次策略。
        #
        # 宣告為 True 的策略，實盤每次鉤子只會拿到**一檔的一筆報價**
        # （`List[StockQuote]` 長度為 1、`scale=TICK`）。需要橫斷面比較
        # （挑最強的前 N 檔）的邏輯必須自己在策略內保存其他標的的最新報價——
        # 逐筆之下沒有人會把全市場一次交給你。
        #
        # **它同時會擋掉 `Scale.TICK` 的整天回測**（見 `Backtester.__init__`）：
        # 同一個 `check_open_signal(stock_quotes)`，實盤拿到長度 1 的 list、
        # TICK 回測卻一次拿到整天的 tick，兩邊的 list 語意根本不同。
        # 兩邊都跑得完、都不報錯，而訊號完全不一樣——那是最難查的一種錯。
        #
        # 與當沖無關：能不能當沖看下方的 `allow_day_trade`。
        self.is_tick_triggered: bool = False  # 盤中逐筆觸發（實盤）
        self.init_capital: float = 0  # Initial capital
        # 同時可持有的最大檔數；**預設 None ＝ 不限制**。
        #
        # **不可改成預設 0**：`order_preprocess.check_max_holdings()` 只把 None
        # 當成不限制，預設 0 會讓**忘記設定的新策略每一張開倉單都被引擎剔除**，
        # 回測跑完是零筆交易、零錯誤訊息。寧可預設不限制（策略自己的 sizer 仍會
        # 把關），也不要用一個看起來像「還沒設定」的值去無聲地擋掉所有交易。
        self.max_holdings: Optional[int] = None

        """
        === Direction Setting ===

        策略只需要決定兩件事：做哪個方向、能不能當沖。

        - `direction`：`LONG`（只做多）／`SHORT`（只做空）／`BOTH`（多空都做）。
          引擎以它為訂單方向的白名單，方向不符的訂單一律剔除並記錄。
          每一張訂單實際的多空仍由訂單自己的 `position_type` 決定，記帳與成本都看訂單。
        - `allow_day_trade`：能否在同一天開倉又平倉同一檔。
          - True：同一根 bar 先處理開倉再處理平倉，當天開的部位當天就可能出場；
            當天開平的股票部位，證交稅以當沖稅率計算。
          - False：先平倉再開倉，當天開的部位最快下一根 bar 才會出場。

        台股放空另有市場專屬的效果，見 `BaseStockStrategy` 的〈Short Setting〉。

        同一根 bar 內先開後平或先平後開由引擎依 `allow_day_trade` 決定，策略不另外設定：
        另開一個欄位只會讓兩個設定互相矛盾（不當沖卻先開後平＝偷偷當沖）。
        """
        self.direction: TradeDirection = TradeDirection.LONG  # 交易方向
        self.allow_day_trade: bool = False  # 能否當沖

        """ === Backtest Setting === """
        self.scale: str = Scale.DAY  # Backtest scale: DAY / TICK
        self.start_date: datetime.date = None  # Optional: 回測起始日
        self.end_date: datetime.date = None  # Optional: 回測結束日

        """
        === Live Setting ===

        **回測完全不讀這一區**，故加上它們不會改變任何回測結果。

        `live_ready` 預設 False：**策略預設不可上實盤**。它不只是一個旗標，
        還帶一條契約——策略的內部狀態必須能由「歷史資料 ＋ 當前帳戶部位」重建，
        不可依賴回測逐日跑出來的累積（例如自行累計的持有天數、只在 `setup()`
        算一次而盤中會過期的清單）。實盤會在任意時點重啟，重建不出來的狀態
        會產生錯訊號，而且不會報錯。標成 True 之前要逐項確認並寫進
        class docstring 的〈實盤執行〉區塊。

        `live_schedule` 宣告各鉤子在哪一個段落被呼叫，例如
        `{"open": AT_OPEN, "close": AT_CLOSE, "stop_loss": AT_CLOSE}`。
        日 K 在實盤不存在——回測一次呼叫就同時拿到當日 OHLC，實盤在開盤前不知道
        close、收盤前不知道完整 OHLC，所以同一支策略的鉤子要拆成兩個時點。

        ⚠️ **`live_schedule` 與 `allow_day_trade` 可能互相矛盾。**
        回測依 `allow_day_trade` 決定同一根 bar 內先開後平還是先平後開；實盤拆成兩段之後，
        **兩個鉤子分屬不同段落時，實際順序由段落決定**：`open` 排在 `close` 之前，
        當天開的部位當天就會被拿去檢查要不要平，等同當沖。兩者不一致時會**靜默**
        改掉交易順序，回測與實盤的部位軌跡從當天起就不同。
        故啟動時一律檢查（見 `core/live/strategy_guard.py`）。

        `live_tag` 是策略代號，**只寫本地紀錄與報表，不送券商**——券商的
        `custom_field` 那 6 個字元讓給委託識別碼的壓縮碼（壓縮碼反查得到策略，
        策略代號卻反查不到是哪一張單），因此它不受 6 字元與英數字的限制。

        `live_execution` 宣告這支策略的委託「要成交」（`MARKET`）還是
        「照價掛單」（`LIMIT`），開倉與平倉共用一個值。**停損一律視為 `MARKET`**，
        不受這裡影響：出場不該因為價格掛不到而失敗。換成券商委託（價格類型、
        委託價、ROD／IOC）由執行層依段落處理，策略不填 `price_type`。
        回測假設「以收盤價成交」的策略應宣告 `MARKET`。
        **沒有預設值**：上實盤的策略沒宣告就在啟動時擋下（見 `strategy_guard`），
        給預設值等於替策略作者決定它要不要成交。
        """
        self.live_ready: bool = False  # 預設不可上實盤
        self.live_schedule: Dict[str, ExecutionTiming] = {}  # 各鉤子的執行段落
        self.live_tag: str = ""  # 策略代號（只寫本地）
        self.live_execution: Optional[ExecutionStyle] = None  # 實盤執行方式

        # 實盤專用的資金額度上限；`None` 表示沿用 `init_capital`。
        #
        # **存在的理由是 `init_capital` 同時是回測帳戶的初始資金**
        # （見 `core/backtest/factory.py`），而 LONG 回歸基準就是拿某支策略跑出來的
        # ——改它等於改掉每一筆回測結果、破壞回歸雙線。實盤帳戶的規模是另一回事：
        # 模擬帳戶、正式帳戶、不同時期的本金都可能與研究時設的數字不同。
        #
        # **回測完全不讀這個屬性**，兩條路徑因此可以各自調整而互不影響。
        self.live_capital: Optional[float] = None

        # 實盤專用的最大持倉檔數；`None` 表示沿用 `max_holdings`。
        #
        # **與 `live_capital` 成對存在**：等權切分的每檔資金是「資金 ÷ 檔數」，
        # 只把資金調小而檔數不動，每檔分到的錢會小到連一張都買不起——
        # sizer 無條件捨去成 0 張，訊號整批被丟掉而不報錯。
        # 同樣不能直接改 `max_holdings`：它也是回歸基準的一部分。
        self.live_max_holdings: Optional[int] = None

    def check_removed_settings(self) -> List[str]:
        """
        - Description:
            找出策略仍在設定的已移除欄位，回傳說明清單（空清單代表沒有）

            回測與實盤啟動時都會呼叫，見 `build_backtester()` 與
            `core/live/strategy_guard.py` 的 `inspect_strategy()`。
        - Return:
            - List[str]
                每個已移除欄位一條說明
        """

        return [
            f"`{name}` 已不再使用：{hint}"
            for name, hint in _REMOVED_SETTINGS.items()
            if name in vars(self)
        ]

    @abstractmethod
    def setup_account(self, account: BaseAccount) -> None:
        """
        - Description:
            載入虛擬帳戶資訊
        """

        pass

    def setup_apis(self, feed: Any) -> None:
        """
        - Description:
            宣告本策略要用的資料源；**預設什麼都不做**

            `BaseDataFeed.setup()` 一律會呼叫它，故**契約必須定義在這一層**，
            不能只長在各市場的策略基底上：否則直接繼承 `BaseStrategy` 的策略
            （例如測試替身，或不吃資料庫的策略）會在 `setup()` 當場 `AttributeError`，
            而那個訊息完全指不到「契約缺了一塊」這個真正的原因。

            各市場的策略基底都會覆寫它。
        - Parameters:
            - feed: Any
                引擎持有的資料源
        """

    # === Alpha 層：策略只需要實作這些 ===
    def generate_open_signals(self, quotes: List[BaseQuote]) -> List[Signal]:
        """
        - Description:
            開倉訊號：選標的、定方向、給價，**不決定數量**

            數量由 portfolio 層依資金或保證金換算（見 `check_open_signal()`），
            故回傳的 `Signal` 一律不填 `volume`。

            **策略不覆寫 `check_open_signal()`**：那是基底提供的引擎契約，
            現行三支正式策略全部只實作 `generate_*_signals()`。覆寫它會讓
            本方法整個不被走到，數量換算與風控前處理也一併被跳過。
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
