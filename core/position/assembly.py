import datetime
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from core.market.tw.cost_model import TwFuturesCostModel, TwStockCostModel
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_margin_model import FuturesMarginModel
from core.models import FuturesAccount, StockAccount
from core.models.cost_config import CostConfig, FuturesCostConfig
from core.position.futures.position_manager import FuturesPositionManager
from core.position.stock.position_manager import StockPositionManager
from core.strategies.base import BaseStrategy
from core.utils import ShortMethod

"""
台股／台期貨的記帳 model 組：回測與實盤共用同一份組法

**為什麼單獨一個模組**：成本設定、保證金模型與部位管理層原本在回測 factory 與實盤 factory
各組一份——實盤一律用預設費率、也沒注入乘數 resolver，同一支策略在兩邊記出不同的帳，
而且不會有任何錯誤訊息。兩邊只能有一份組法；放在部位層而不放回測 factory，
實盤才不必依賴回測。市場專屬的其他元件（資料源、成交模型、執行層）仍由各自的 factory 組。
"""


@dataclass
class TwStockModels:
    """台股的記帳 model 組"""

    cost_config: CostConfig
    cost_model: TwStockCostModel
    position_manager: StockPositionManager


@dataclass
class TwFuturesModels:
    """台期貨的記帳 model 組；`margin_config` 與 `margin_model` 已回寫給策略"""

    cost_config: FuturesCostConfig
    margin_config: FuturesMarginConfig
    cost_model: TwFuturesCostModel
    margin_model: FuturesMarginModel
    position_manager: FuturesPositionManager


def build_cost_config(strategy: BaseStrategy) -> CostConfig:
    """
    - Description:
        依台股策略的宣告推導成本設定：允許當沖時空單一律走現股當沖沖賣

        **回測與實盤都走這一份**：原本實盤一律 `CostConfig.default()`，當沖策略的空單
        在實盤記成融券、`is_day_trade` 恆為 False，日終當沖回補因此認不出它們。

        以 `getattr` 讀策略欄位：實盤測試的最小替身只繼承 `BaseStrategy`，沒有
        `short_method`／`short_constraint`；缺的欄位照台股基底的預設值處理。
    - Parameters:
        - strategy: BaseStrategy
            台股策略（`BaseStockStrategy`）
    - Return:
        - CostConfig
            這支策略記帳用的成本參數
    """

    # 做多的當沖稅率也由這個旗標開啟：平倉時另外比對開平倉是否同一天才減半，
    # 見 `StockPositionManager.close_long_position()`
    is_day_trade: bool = bool(getattr(strategy, "allow_day_trade", False))
    short_method: ShortMethod = (
        ShortMethod.DAY_TRADE
        if is_day_trade
        else getattr(strategy, "short_method", ShortMethod.MARGIN)
    )

    config: CostConfig = getattr(strategy, "cost_config", None) or CostConfig.default(
        short_method, is_day_trade
    )

    short_constraint = getattr(strategy, "short_constraint", None)
    if short_constraint is not None:
        config.short_constraint = short_constraint

    # 回測區間交給 CostModel 做稅制邊界的警示（當沖證交稅減半有實施日與落日日期）。
    # 由組裝層注入是因為策略才知道自己要跑哪一段，而 CostModel 不該反向去問策略
    config.backtest_start_date = getattr(strategy, "start_date", None)
    config.backtest_end_date = getattr(strategy, "end_date", None)

    return config


def build_tw_stock_models(
    strategy: BaseStrategy,
    account: StockAccount,
    event_counts: Optional[Dict[str, int]] = None,
) -> TwStockModels:
    """
    - Description:
        組台股的成本模型與部位管理層
    - Parameters:
        - strategy: BaseStrategy
            台股策略
        - account: StockAccount
            帳戶（回測用研究本金、實盤用 `live_capital`，由呼叫端決定）
        - event_counts: Optional[Dict[str, int]]
            事件計數；回測與 FillModel 共用同一個 dict，實盤不計
    - Return:
        - TwStockModels
            成本設定、成本模型、部位管理層
    """

    cost_config: CostConfig = build_cost_config(strategy)
    cost_model: TwStockCostModel = TwStockCostModel(cost_config)
    position_manager: StockPositionManager = StockPositionManager(
        account, cost_model, event_counts=event_counts
    )
    return TwStockModels(cost_config, cost_model, position_manager)


def build_tw_futures_models(
    strategy: BaseStrategy,
    account: FuturesAccount,
    multiplier_resolver: Optional[Callable[[str, datetime.date], int]] = None,
    underlying_price_resolver: Optional[
        Callable[[str, datetime.date], Optional[float]]
    ] = None,
    event_counts: Optional[Dict[str, int]] = None,
) -> TwFuturesModels:
    """
    - Description:
        組台期貨的成本模型、保證金模型與部位管理層，並把保證金設定與模型回寫給策略

        **回寫是必要的**：策略層的 sizer 算可開口數、部位管理層算應繳保證金，兩者必須是
        同一個設定物件與同一個模型——否則策略算得出口數、部位管理層卻開不進去，而且不會
        有任何錯誤訊息。保證金 API 由資料源稍後注入這同一個設定物件。

        乘數與標的股價的 resolver 由資料源提供：指數期貨查常數表、股票期貨逐日查契約單位，
        未注入時退回常數表（純記憶體測試只跑指數期貨，常數表夠用；股期第一筆開倉會 KeyError，
        那是刻意的）。
    - Parameters:
        - strategy: BaseStrategy
            台期貨策略
        - account: FuturesAccount
            帳戶
        - multiplier_resolver: Optional[Callable[[str, datetime.date], int]]
            契約乘數 resolver（資料源的 `resolve_multiplier`）
        - underlying_price_resolver: Optional[Callable[[str, datetime.date], Optional[float]]]
            股期標的股價 resolver（比例表要用）；實盤目前沒有來源，給 None
        - event_counts: Optional[Dict[str, int]]
            事件計數
    - Return:
        - TwFuturesModels
            成本與保證金設定、成本模型、保證金模型、部位管理層
    """

    cost_config: FuturesCostConfig = (
        getattr(strategy, "cost_config", None) or FuturesCostConfig.default()
    )
    margin_config: FuturesMarginConfig = (
        getattr(strategy, "margin_config", None) or FuturesMarginConfig.default()
    )
    strategy.margin_config = margin_config

    cost_model: TwFuturesCostModel = TwFuturesCostModel(cost_config)
    margin_model: FuturesMarginModel = FuturesMarginModel(
        margin_config, underlying_price_resolver=underlying_price_resolver
    )
    strategy.margin_model = margin_model

    position_manager: FuturesPositionManager = FuturesPositionManager(
        account,
        cost_model=cost_model,
        margin_config=margin_config,
        multiplier_resolver=multiplier_resolver,
        underlying_price_resolver=underlying_price_resolver,
        event_counts=event_counts,
        margin_model=margin_model,
    )
    return TwFuturesModels(
        cost_config, margin_config, cost_model, margin_model, position_manager
    )
