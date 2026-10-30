from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from core.live.account_sync import FilledOrderBuilder
from core.live.datafeed.base import BaseLiveDataFeed
from core.live.execution.base import BaseExecutionModel
from core.market.cost_model import BaseCostModel
from core.models import BaseAccount, BaseOrder
from core.models.risk_config import BaseRiskConfig, default_risk_config
from core.position.base.position_manager import BasePositionManager
from core.strategies.base import BaseStrategy

"""
StrategyContext：一支策略在實盤跑起來需要的一整組東西

**獨立成一個模組而不是留在引擎本體**：引擎拆成多個協作元件後，每個元件都要
引用它；放在引擎本體裡的話，元件 import 引擎、引擎又 import 元件，是檔案層級的循環。
"""


@dataclass
class StrategyContext:
    """
    - Description:
        一支策略跑起來需要的一整組東西

        **每支策略一份**。共用一份的話，兩支策略的部位與損益會混在一起，
        而合計仍然正確——對帳看不出來。
    """

    strategy: BaseStrategy
    account: BaseAccount
    position_manager: BasePositionManager
    data_feed: BaseLiveDataFeed
    # 成本模型：委託前處理用它補市場專屬欄位（台股空單的放空管道與當沖旗標），
    # 與本地帳記帳用的是同一個；None 時不補值（純記憶體測試）
    cost_model: Optional[BaseCostModel] = None
    # 本策略要處理的標的；實盤不掃全市場，標的池由策略決定
    symbols: List[str] = field(default_factory=list)
    # 把一張委託換算成金額（風控與資金保留都要用）。
    # 計價單位是市場特性（張要乘 1000 股、口要乘保證金），故由外部注入
    calculate_notional: Optional[Callable[[BaseOrder], float]] = None
    # 把「已成交的一筆」還原成訂單物件餵給 `PositionManager`。
    # 訂單型別同樣是市場特性（期貨要 product／expiry），故一併由外部注入
    build_filled_order: Optional[FilledOrderBuilder] = None
    # 開一筆倉需要的資金（保證金交易的商品：原始保證金 ＋ 開倉成本）；None 代表
    # 這個市場送單前不做保證金檢查（現股的資金由額度分配與風控管）
    calculate_opening_requirement: Optional[Callable[[BaseOrder], float]] = None
    # 執行層：把執行方式換成券商委託。換算規則是市場特性（集合競價的時段、
    # 連續交易時段的保護方式），故由外部注入；未注入時本策略的委託一律不送
    execution_model: Optional[BaseExecutionModel] = None
    # 把一張委託換算成曝險（批次曝險上限要用）。股票與 `calculate_notional` 相同；
    # 期貨是契約價值（點數 × 乘數 × 口數），與資金占用的保證金不同——
    # 曝險要反映「承擔多少風險」，保證金只反映「押了多少錢」。未注入時退回 `notional()`
    calculate_exposure: Optional[Callable[[BaseOrder], float]] = None
    # 本策略的風控門檻（策略宣告、部署設定收緊後的結果）；None 時用該市場的預設值
    risk_config: Optional[BaseRiskConfig] = None
    # 本段落餵給策略的報價：symbol → 最新成交價（盤前段為參考價）。
    # 風控的決策價偏離檢查拿它當市場基準；策略決定要買 108 時，市場在 100 還是 107
    # 只有這份報價知道。每個段落重取，不跨段落沿用
    quote_prices: Dict[str, float] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """策略名；歸屬鏈以它為鍵"""

        return type(self.strategy).__name__

    @property
    def capital_limit(self) -> float:
        """
        - Description:
            本策略的實盤資金額度，風控各道上限的基準

            取帳戶的初始資金：實盤帳戶由組裝層以 `live_capital` 建立，
            與資金額度分配同一個值。**不可用 `strategy.init_capital`**——
            那是研究回測的本金，宣告了較小 `live_capital` 的策略會讓每道上限
            照較大的本金放寬。
        """

        return float(self.account.init_capital)

    @property
    def risk_limits(self) -> BaseRiskConfig:
        """本策略實際套用的風控門檻；沒有指定時取該市場的預設值"""

        if self.risk_config is not None:
            return self.risk_config
        return default_risk_config(getattr(self.strategy, "instrument_type", None))

    def notional(self, order: BaseOrder) -> float:
        """
        本張委託的資金占用（股票：價 × 股數；期貨：原始保證金）；
        未注入換算器時退回「價 × 量」
        """

        if self.calculate_notional is not None:
            return self.calculate_notional(order)
        return float(order.price) * float(order.volume)

    def exposure(self, order: BaseOrder) -> float:
        """本張委託的曝險（股票：價 × 股數；期貨：契約價值）；未注入時同 `notional()`"""

        if self.calculate_exposure is not None:
            return self.calculate_exposure(order)
        return self.notional(order)

    def position_value(self) -> float:
        """
        - Description:
            本策略未平倉部位的資金占用（股票：開倉價 × 股數；期貨：原始保證金）

            **要經過與委託相同的換算器**：部位的數量台股是張、期貨是口，只做「價 × 量」
            的話台股少乘 1000 股——已用額度因此小了一千倍，持倉幾乎不佔額度，
            而那不會有任何錯誤訊息。換算器只讀 `price` 與 `volume`，部位兩者都有。

            **口徑是開倉成本不是即時市值**：本地帳沒有即時報價，硬要取市值就得在每個
            呼叫點各查一次行情，而帳務類限流只有 25 次／5 秒。它只用在額度與占用這類
            「分母」上——分母漏掉整批持倉會讓超配變成通過，落後一段行情不會。
        - Return:
            - float
                帳面金額
        """

        return float(
            sum(
                self.notional(position)
                for position in self.account.positions
                if not position.is_closed
            )
        )
