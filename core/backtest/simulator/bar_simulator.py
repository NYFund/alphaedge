import datetime
from typing import List

from core.backtest.simulator.base import BaseSimulator, SimulationEngine
from core.models import BaseQuote
from core.utils import BarExecutionSequence, Scale

"""日 K simulator：一天一根 bar，訊號與成交都在同一根 bar 內完成"""


class BarSimulator(BaseSimulator):
    """
    - Description:
        一天一根 bar：整天的報價一次交給策略，依 `allow_day_trade` 推導的先後開平倉，當根成交

        台股與期貨共用：市場差異全在注入引擎的 model 裡，本類別不分市場。
        只處理 `Scale.DAY`；逐筆由 `TickSimulator` 負責。
    """

    def __init__(self, engine: SimulationEngine) -> None:
        """
        - Description:
            建立日 K simulator
        - Parameters:
            - engine: SimulationEngine
                引擎
        """

        super().__init__(engine)

        # `load_day()` 取到的當天報價，`run_day()` 用完即清掉，不留到下一個交易日
        self.quotes: List[BaseQuote] = []

    def load_day(self, date: datetime.date) -> bool:
        """
        - Description:
            取得當天的報價；DAY 依引擎設定決定是否用還原價
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - bool
                False 表示當天沒有任何報價，整天跳過
        """

        self.quotes = self.engine.data_feed.get_quotes(
            date, Scale.DAY, adjusted=self.engine.adjusted_price
        )
        return bool(self.quotes)

    def run_day(self, date: datetime.date) -> List[BaseQuote]:
        """
        - Description:
            把當天唯一的一根 bar 交給 `process_bar()`
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - List[BaseQuote]
                同一根 bar 的報價，也就是收盤報價
        """

        quotes: List[BaseQuote] = self.quotes
        self.quotes = []

        self.process_bar(quotes)
        return quotes

    def process_bar(self, quotes: List[BaseQuote]) -> None:
        """
        - Description:
            依執行順序對一根 bar 開平倉

            **同一標的同時出現在開倉與平倉訊號時，兩腿分別成交，不做 net 合併。**
            理由是成本與損益歸屬：證交稅只課賣出腿、當沖稅率減半也只認當沖的那一腿，
            合併成一張淨額委託會讓兩腿的費用與稅無法各自計算；且平倉腿必須實際成交
            才會產生 `TradeRecord`，net 掉等於整筆交易在報表上消失。
            兩腿的先後完全由執行順序決定（依策略的 `allow_day_trade` 推導）——這正是它存在的理由：

            - `OPEN_THEN_CLOSE`：先開後平，同一根 bar 內可完成當沖來回。
            - `CLOSE_THEN_OPEN`：先平後開，同一標的是「先出清舊倉再重新建倉」。

            同一階段內同一標的的多筆委託則依到達順序逐筆處理
            （`sort_orders()` 為穩定排序），支援分批建倉與部分平倉。
        - Parameters:
            - quotes: List[BaseQuote]
                當根 bar 的報價
        """

        if self.engine.get_execution_sequence() == BarExecutionSequence.OPEN_THEN_CLOSE:
            self.execute_open_signal(quotes)
            self.execute_close_signal(quotes)
        else:
            self.execute_close_signal(quotes)
            self.execute_open_signal(quotes)
