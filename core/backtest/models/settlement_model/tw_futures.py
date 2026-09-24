import datetime
from typing import Any, Dict, List, Optional

from loguru import logger

from core.backtest.models.fill_model import BaseFillModel
from core.backtest.models.instrument_spec import (
    InstrumentSpec,
    TwFuturesSpec,
)
from core.backtest.models.settlement_model.base import BaseSettlementModel
from core.managers.futures.position_manager import FuturesPositionManager
from core.market.tw.futures_margin_config import FuturesMarginConfig
from core.market.tw.futures_roll import FuturesRollConfig, FuturesRollPlanner
from core.models import (
    BaseAccount,
    FuturesAccount,
    FuturesOrder,
    FuturesPosition,
    FuturesQuote,
    FuturesTradeRecord,
)
from core.utils import (
    Action,
    MarginCallPolicy,
    PositionType,
)

"""TwFuturesSettlementModel: 台期貨的逐日盯市、保證金追繳與到期換月"""


class TwFuturesSettlementModel(BaseSettlementModel):
    """
    台期貨結算模型：**每日以結算價逐日盯市**

    台股在此掛點做的是「當沖日終強制回補」，期貨做的是「每日結算」。

    **逐日盯市的記帳語意**（與股票最根本的差異）：損益不等到平倉才實現，
    每個交易日以結算價結清當日損益、現金當天就進出帳戶，部位的 `price`
    隨之重設為結算價。實作在 `FuturesPositionManager.settle_daily()`。

    ---

    **保證金追繳、換月轉倉與交易日曆**分別由本 model 的追繳檢查、`FuturesRollPlanner`
    與 `FuturesCalendar` 處理，細節見各自的 docstring。

    ⚠️ **到期契約的權宜出場**：已到期的契約不會再有報價，策略因此拿不到報價、
    也就下不出那張平倉單——不處理的話該部位會一路留到回測結束並持續佔用保證金
    （實測：示範策略在 2024-04 開的近月部位卡到 12 月，凍結 79 萬保證金）。
    故本 model 在契約連續 `MAX_NO_QUOTE_DAYS` 根 bar 沒有報價時，以**最近一次
    結算價**強制出場並計入 `forced_cover_no_quote`。這是**兜底不是換月**：
    換月轉倉會先把月契約部位轉走，本段接住的是轉倉接不到的部位（例如週契約）。

    ⚠️ **強制平倉與換月轉倉都走 `apply_fill_price()`**：追繳、到期兜底與轉倉的
    兩腿吃的是與策略同一組 `fill_config`，故 log 印的「以 X 強制平倉」是**滑價前的
    參考價**，實際入帳價另含滑價（轉倉的 log 則印含滑價後的價格，因為那是新部位的
    成本）。設定裡沒開滑價時兩者相同。
    """

    # 契約連續幾根 bar 沒有報價就強制出場。
    #
    # **不設成 1**：單日的資料缺漏（爬蟲漏一天、當日零成交）與「契約已到期」
    # 在報價層看起來一模一樣，設成 1 會讓前者被誤判成到期而提早平倉。
    # 到期契約晚幾天出場不影響損益——盯市價已凍結在最後一次結算價，
    # 那幾天的每日結算損益都是 0。
    MAX_NO_QUOTE_DAYS: int = 3

    def __init__(
        self,
        position_manager: FuturesPositionManager,
        instrument: Optional[InstrumentSpec] = None,
        roll_config: Optional[FuturesRollConfig] = None,
        fill_model: Optional[BaseFillModel] = None,
    ) -> None:
        super().__init__(fill_model=fill_model)

        self.position_manager: FuturesPositionManager = position_manager
        self.instrument: InstrumentSpec = instrument or TwFuturesSpec()

        # 換月設定；`calendar` 由 DataFeed 注入同一個物件（見 `FuturesRollConfig`）
        self.roll_config: FuturesRollConfig = roll_config or FuturesRollConfig()

        # {契約代號: 連續無報價的 bar 數}；同一契約的多個部位共用同一個計數
        self.no_quote_days: Dict[str, int] = {}

    @property
    def margin_config(self) -> FuturesMarginConfig:
        """保證金設定；唯一來源是部位管理層持有的那一份，不另存副本"""

        return self.position_manager.margin_config

    def on_bar_close(
        self,
        date: datetime.date,
        quotes: List[FuturesQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            一根 bar 收盤後逐日盯市：以當日結算價結清每個未平倉部位的當日損益

            保證金追繳的強制平倉計入 `forced_cover_margin_call`，
            到期兜底出場計入 `forced_cover_no_quote`。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[FuturesQuote]
                當根 bar 的報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數；轉交給換月、到期出場與追繳三個步驟
        """

        quote_map: Dict[str, FuturesQuote] = {quote.symbol: quote for quote in quotes}

        for position in list(account.get_positions()):
            self.position_manager.settle_daily(
                position, self.get_mark_price(position, quote_map)
            )

        # 換月排在到期出場之前：能轉倉就轉倉，轉不了才走權宜出場。
        # 順序對調的話，所有部位都會先被當成「到期」平掉，換月永遠不會發生
        self.roll_positions(date, quotes, account, event_counts)

        # 逐日盯市完才處理到期出場：出場價即最近一次結算價，
        # 先結算才不會漏掉最後一根 bar 的損益
        self.close_expired_positions(date, quote_map, account, event_counts)

        # 追繳放最後：前面兩步都會改變權益與佔用保證金，先判斷會用到過期的數字
        self.check_margin_call(date, quote_map, account, event_counts)

    def check_margin_call(
        self,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
        account: FuturesAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            保證金追繳：帳戶**權益**低於維持保證金總額時，依政策強制平倉或僅標記

            **判斷的是權益不是可動用餘額**（權益 ＝ 可動用餘額 ＋ 佔用保證金）：
            期貨的浮動損益每日結算進帳戶，虧損會先吃掉可動用餘額，可動用餘額歸零
            並不代表已經被追繳——真正的門檻是「權益是否還撐得住維持保證金」。
            反過來說，浮動獲利會讓權益上升，因而**可以支撐加碼**，
            這正是本步驟要求「以權益決定保證金充足度」的意思。

            **強制平倉逐口處理、由保證金最大的部位開始**：先平掉佔用最多的那一口
            才可能一次把權益拉回門檻之上；每平一筆就重算，避免一次全砍
            （真實券商的斷頭也是砍到足額為止，不是清空帳戶）。

            出場價一律用當日盯市價（＝結算價）。**現行引擎沒有跨日委託佇列**，
            無法模擬「次一交易日開盤成交」，與台股的維持率追繳同一種簡化。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - account: FuturesAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        # 每一輪都重新取部位：`close_position()` 走 FIFO，被平掉的不一定是本輪挑中的
        # 那一筆（同一契約的多筆部位共用同一個 symbol），拿舊清單續跑會重複計數
        for _ in range(len(account.get_positions()) + 1):
            positions: List[FuturesPosition] = account.get_positions()
            if not positions:
                return

            requirement: float = self.get_maintenance_requirement(date, positions)
            if account.equity >= requirement * self.margin_config.margin_call_ratio:
                return

            if self.margin_config.margin_call_policy == MarginCallPolicy.WARN_ONLY:
                # **不計數**：`forced_cover_margin_call` 的語意是「強制平倉幾次」，
                # 只標記卻計數會讓報表把「撐過去了」讀成「被斷頭了」。
                # 且本狀態每根 bar 都會成立，計數會隨天數膨脹（與台股同一種處理）
                logger.warning(
                    f"[Margin Call] {date} 權益 {account.equity:.0f} 低於維持保證金 "
                    f"{requirement:.0f}（僅標記不平倉）"
                )
                return

            # 先砍佔用保證金最多的契約，才可能一次把權益拉回門檻之上；
            # 同一契約內由誰出場則由 FIFO 決定（每口佔用相同，結果等價）
            position: FuturesPosition = max(positions, key=lambda p: p.margin)
            price: float = self.get_mark_price(position, quote_map)
            logger.warning(
                f"[Margin Call] {position.symbol} 權益 {account.equity:.0f} 低於"
                f"維持保證金 {requirement:.0f}，以 {price} 強制平倉"
            )
            event_counts["forced_cover_margin_call"] += 1

            if not self.close_position_at(
                position, date, price, quote_map.get(position.symbol)
            ):
                # 平不掉就停手，否則會在同一根 bar 內無限重試
                logger.warning(
                    f"[Margin Call] {position.symbol} 無法平倉，本根 bar 停止追繳處理"
                )
                return

    def get_maintenance_requirement(
        self, date: datetime.date, positions: List[FuturesPosition]
    ) -> float:
        """所有未平倉部位在該日的維持保證金總額（逐商品查表，見部位管理層）"""

        return sum(
            self.position_manager.calculate_maintenance_margin(position, date)
            for position in positions
        )

    def roll_positions(
        self,
        date: datetime.date,
        quotes: List[FuturesQuote],
        account: FuturesAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            換月：把未平倉部位轉到規則指定的當家契約

            **換月是市場結構強加的，不是策略訊號**：契約會到期，部位不轉倉就會
            憑空消失，因此放在結算模型而不是策略層。但「什麼時候轉」是政策，
            由 `FuturesRollConfig.rule` 決定，且**與策略挑合約用同一份規則實作**
            （`FuturesRollPlanner`）——兩處不一致會讓訊號在次月、部位在近月。

            **轉倉 ＝ 平掉舊契約 ＋ 以相同口數與方向開新契約**：舊契約以盯市價
            平倉（損益已逐日結清，這一段通常為 0），新契約以當日收盤價開倉。
            展期價差因此**真實反映在帳戶上**——這正是連續合約要調整掉的那筆錢，
            回測不該把它變不見。

            **開不進去就只記 warning 不還原**：新契約的保證金可能因調整而變高，
            此時曝險會少掉——那是真實會發生的事（真的繳不出保證金就是轉不了倉），
            靜默還原成舊部位反而是造假。

            ⚠️ **轉倉後持有天數重新計算**：新部位的開倉日是轉倉日。策略若以
            「持有滿 N 天才平倉」為出場條件，轉倉會讓那個計時重來一次。
            這與真實情況一致（那確實是一筆新的部位），但用持有天數當出場條件的
            策略要自己意識到這件事。
        - Parameters:
            - date: datetime.date
                當前交易日
            - quotes: List[FuturesQuote]
                當根 bar 的報價
            - account: FuturesAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        if not self.roll_config.enabled:
            return

        planner: Optional[FuturesRollPlanner] = self.roll_config.build_planner()
        if planner is None or not account.get_positions():
            return

        quote_map: Dict[str, FuturesQuote] = {quote.symbol: quote for quote in quotes}
        expiries: Dict[str, List[str]] = {}
        open_interest: Dict[str, Dict[str, Any]] = {}
        for quote in quotes:
            expiries.setdefault(quote.product, []).append(quote.expiry)
            open_interest.setdefault(quote.product, {})[quote.expiry] = (
                quote.open_interest
            )

        for position in list(account.get_positions()):
            # **週契約不由本規則轉倉**：規劃器只認月契約，硬轉會把週契約的部位
            # 換成月契約——那是不同的商品，不是同一條曝險的延續
            if not planner.MONTHLY_EXPIRY_PATTERN.match(position.expiry):
                continue

            active: Optional[str] = planner.resolve_active_expiry(
                date,
                expiries.get(position.product, []),
                open_interest.get(position.product),
            )
            # **不可換到比現在更近的月份**：`OPEN_INTEREST` 規則
            # 比較的是「次月未沖銷量是否超過近月」，而未沖銷量會逐日波動——
            # 換到次月之後，近月的未沖銷量可能又反超一天，於是部位被換回去。
            # 每來回一次就付兩次手續費與一次展期價差，而且是憑空產生的。
            # 換月是單向的：只往遠月走。
            if active is None or active <= position.expiry:
                continue

            new_quote: Optional[FuturesQuote] = quote_map.get(
                f"{position.product}{active}"
            )
            if new_quote is None or not new_quote.close:
                logger.warning(
                    f"[Roll] {position.symbol} 應轉倉至 {active}，但新契約當日無報價，"
                    f"本次不轉倉"
                )
                continue

            self.roll_single_position(position, new_quote, date, quote_map)
            # 引擎的 `new_event_counts()` 沒有這個 key（那是台股的清單），
            # 期貨場次自行加入；台股的事件報表因此完全不受影響
            event_counts["rolled_contract"] = event_counts.get("rolled_contract", 0) + 1

    def roll_single_position(
        self,
        position: FuturesPosition,
        new_quote: FuturesQuote,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
    ) -> None:
        """
        - Description:
            平掉舊契約並以相同口數與方向開新契約（展期價差如實入帳）

            **兩腿都吃滑價**：實盤換月是真的送兩張單、吃兩次價差，只算平倉腿
            會讓轉倉成本少一半。滑價沿用策略既有的 `fill_config`，不另開旋鈕。

            **兩腿一定要用同一種價**（一律走 `get_quote_mark_price()`）：
            結算價與收盤價在期貨是兩個不同的數字，舊腿用結算價、新腿用 `close`
            會讓帳上多出一筆**不存在的展期價差**——而展期價差正是這裡唯一該
            記錄的東西，摻進口徑差異就失去意義了。
        - Parameters:
            - position: FuturesPosition
                要轉倉的部位
            - new_quote: FuturesQuote
                新契約當日報價
            - date: datetime.date
                轉倉日
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
        """

        volume: int = position.volume
        position_type: PositionType = position.position_type
        exit_price: float = self.get_mark_price(position, quote_map)
        entry_price: float = self.get_quote_mark_price(new_quote)

        self.close_position_at(
            position, date, exit_price, quote_map.get(position.symbol)
        )

        # 開新月這一腿同樣要吃滑價（理由見 docstring）
        entry_order: FuturesOrder = self.apply_fill_price(
            FuturesOrder(
                product=new_quote.product,
                expiry=new_quote.expiry,
                date=date,
                action=(
                    Action.BUY if position_type == PositionType.LONG else Action.SELL
                ),
                position_type=position_type,
                price=entry_price,
                volume=volume,
            ),
            new_quote,
        )

        # log 印**含滑價後**的價格，否則與實際入帳價不一致
        logger.info(
            f"* Roll {position.symbol} → {new_quote.contract_id} "
            f"({volume} lots @ {entry_order.price})"
        )

        opened = self.position_manager.open_position(entry_order)
        if opened is None:
            logger.warning(
                f"[Roll] {new_quote.contract_id} 開倉失敗（多半是保證金不足），"
                f"原本 {volume} 口的曝險已消失"
            )

    def close_expired_positions(
        self,
        date: datetime.date,
        quote_map: Dict[str, FuturesQuote],
        account: BaseAccount,
        event_counts: Dict[str, int],
    ) -> None:
        """
        - Description:
            把已停止交易（連續無報價）的契約以最近一次結算價強制出場

            **為什麼非做不可**：策略是靠報價下單的，契約到期後不再有報價，
            策略永遠下不出那張平倉單，部位會留到回測結束並持續佔用保證金。

            出場價取 `position.price`——逐日盯市之後它就是最近一次結算價，
            該部位到期前的損益早已逐日結進帳戶，故這一段的價差為 0（有設滑價時
            另扣一段，見 `apply_fill_price()`；**這條路徑沒有報價可做區間檢查**）。
            與真正的最終結算價（最後交易日次一營業日的特別開盤參考價）仍有落差，
            **正常情況下換月轉倉會先把部位轉走，這段是兜底**。
        - Parameters:
            - date: datetime.date
                當前交易日（＝出場日；會比實際最後交易日晚幾根 bar）
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - account: BaseAccount
                交易帳戶
            - event_counts: Dict[str, int]
                事件計數
        """

        self.update_no_quote_days(quote_map, account.get_positions())

        for position in list(account.get_positions()):
            if self.no_quote_days.get(position.symbol, 0) < self.MAX_NO_QUOTE_DAYS:
                continue

            logger.warning(
                f"[Expired] {position.symbol} 連續 {self.MAX_NO_QUOTE_DAYS} 根 bar "
                f"無報價（契約已到期），以最近一次結算價 {position.price} 強制出場"
            )
            event_counts["forced_cover_no_quote"] += 1
            # 走到這裡的定義就是「連續無報價」，故一律傳 None 跳過區間檢查
            self.close_position_at(position, date, position.price, None)

    def close_position_at(
        self,
        position: FuturesPosition,
        date: datetime.date,
        price: float,
        quote: Optional[FuturesQuote],
    ) -> List[FuturesTradeRecord]:
        """
        - Description:
            以指定價格強制平掉部位（多單賣出、空單買進回補）

            **追繳、到期兜底與轉倉的平倉腿共用這一條**，滑價因此只接一次；
            三者要拆開不同口徑的話得先在此處分流，而不是各自繞過 `apply_fill_price()`。
        - Parameters:
            - position: FuturesPosition
                要平掉的部位
            - date: datetime.date
                成交日
            - price: float
                平倉參考價（滑價前，一般為當日結算價）
            - quote: Optional[FuturesQuote]
                當根 bar 的報價，供區間檢查；到期無報價那條路徑傳 `None`
        - Return:
            - List[FuturesTradeRecord]
                平倉產生的交易紀錄
        """

        order: FuturesOrder = FuturesOrder(
            product=position.product,
            expiry=position.expiry,
            date=date,
            action=(
                Action.SELL
                if position.position_type == PositionType.LONG
                else Action.BUY
            ),
            position_type=position.position_type,
            price=price,
            volume=position.volume,
        )
        return self.position_manager.close_position(self.apply_fill_price(order, quote))

    def update_no_quote_days(
        self,
        quote_map: Dict[str, FuturesQuote],
        positions: List[FuturesPosition],
    ) -> None:
        """
        - Description:
            更新每個契約的連續無報價 bar 數（有報價即歸零）

            **計數掛在 model 而不是部位上**：無報價是**契約**的狀態不是部位的狀態，
            同一契約的多個部位共用同一個答案，記在部位上只會存好幾份一樣的數。
        - Parameters:
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價
            - positions: List[FuturesPosition]
                目前的未平倉部位
        """

        for position in positions:
            quote: Optional[FuturesQuote] = quote_map.get(position.symbol)
            if quote is not None and (quote.close or quote.cur_price):
                self.no_quote_days[position.symbol] = 0
            else:
                self.no_quote_days[position.symbol] = (
                    self.no_quote_days.get(position.symbol, 0) + 1
                )

    def get_mark_price(
        self, position: FuturesPosition, quote_map: Dict[str, FuturesQuote]
    ) -> float:
        """
        - Description:
            取得盯市價：**期貨的盯市價就是當日結算價**

            結算價缺漏時退回收盤價——夜盤本來就沒有結算價（來源即為 NULL），
            日盤偶有缺漏。**不可當成 0**：那會讓部位在一天內被結算成歸零。

            當日完全無報價（契約已到期、或資料缺這一天）時沿用
            `position.price`——逐日盯市之後它就是**最近一次結算價**
            （尚未結算過則為開倉價），見 `FuturesPosition`。
        - Parameters:
            - position: FuturesPosition
                待盯市的部位
            - quote_map: Dict[str, FuturesQuote]
                當根 bar 的報價（以契約代號為鍵）
        - Return:
            - float
                盯市價格
        """

        quote: Optional[FuturesQuote] = quote_map.get(position.symbol)

        if quote is not None:
            price: Optional[float] = self.get_quote_mark_price(quote)
            if price:
                return float(price)

        logger.warning(
            f"[Mark Price] {position.symbol} 當日無結算價可用，"
            f"沿用最近一次結算價 {position.price} 盯市"
        )
        return position.price

    @staticmethod
    def get_quote_mark_price(quote: FuturesQuote) -> float:
        """
        - Description:
            一筆報價的盯市價：結算價優先，缺漏時退回收盤價

            抽出來是為了讓**轉倉的兩腿用同一種價**：舊腿走盯市價、
            新腿走 `close` 的話，帳上會多出一筆不存在的展期價差。
        - Parameters:
            - quote: FuturesQuote
                報價
        - Return:
            - float
                盯市價；三種價格都沒有時為 0.0
        """

        price: Optional[float] = (
            quote.settlement_price
            if quote.settlement_price is not None
            else (quote.close or quote.cur_price)
        )
        return float(price) if price else 0.0

    def mark_position(
        self, position: FuturesPosition, mark_price: float, units: int
    ) -> float:
        """
        - Description:
            期貨的部位價值 ＝ **保證金 ＋ 尚未結算的那一段損益**

            **不是契約價值**：保證金交易只凍結保證金，契約價值本身不佔用資金
            （TX 一口契約價值 900 萬、保證金只有 70 萬），沿用基底的現金帳戶口徑
            會讓權益曲線整段偏高一個數量級。

            `on_bar_close()` 的逐日盯市已把當日損益結進 `balance`，故本方法在
            多數日子算出的未實現損益是 **0——那是對的，不是沒算到**；
            只有當日無結算價（沿用舊價）或報價缺漏時才會有殘值。

            **`units` 用不到**：期貨的乘數逐契約不同，`InstrumentSpec.to_units()`
            拿不到商品（見 `TwFuturesSpec`），乘數一律取自部位自身的
            `multiplier`，損益公式直接走 `FuturesPositionManager.calculate_pnl()`。
        - Parameters:
            - position: FuturesPosition
                未平倉部位；`unrealized_pnl` 與 `unrealized_roi` 會被就地更新
            - mark_price: float
                盯市價（＝當日結算價）
            - units: int
                計價單位數量；期貨不使用，見上
        - Return:
            - float
                該部位計入當日權益的金額
        """

        unrealized_pnl: float = self.position_manager.calculate_pnl(
            position_type=position.position_type,
            entry_price=position.price,
            exit_price=mark_price,
            volume=position.volume,
            multiplier=position.multiplier,
        )
        position.unrealized_pnl = round(unrealized_pnl, 2)

        # 報酬率的分母是保證金不是契約價值（期貨投入的資金就是保證金）
        position.unrealized_roi = (
            round(position.unrealized_pnl / position.margin * 100, 2)
            if position.margin
            else 0.0
        )

        return position.margin + position.unrealized_pnl
