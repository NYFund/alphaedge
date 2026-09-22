import datetime
from abc import ABC, abstractmethod
from typing import Dict, List, Set

from core.models import BaseQuote
from core.strategies.base import BaseStrategy
from core.utils import Scale

"""
BaseDataFeed: 資料載入、報價轉換與交易日判定

**引擎與資料源之間的共用契約**：回測與實盤都吃這個介面，所以它不屬於
`core/backtest/`——放在那裡的話，實盤與策略層為了拿一個介面就得 import 回測套件。

〈三種方法的判準〉本檔是基底類別的範本，新增方法前先決定它屬於哪一種：

1. **真抽象（`@abstractmethod`，不給實作）**：沒有任何通用答案、每個市場都不同，
   而且**少了它引擎就跑不動**。`setup()`（哪些 API 要建）與
   `is_market_open()`（交易日怎麼判）屬於這類。
2. **具體實作（基底就寫完）**：邏輯與市場無關，每個子類抄一次只會抄出分歧。
   有共用骨架要收的流程也放這裡，把可變的部分留給第 3 種。
3. **有預設的 hook（基底給一個安全的預設）**：多數市場不需要，少數市場才覆寫。
   `close()` 預設 no-op、`get_price_limit_basis()` 預設回空 dict 都是——
   **預設值必須是「安全」而不是「常見」**：沒有除權息公告的市場回空 dict 會少調整
   幾檔的漲跌停基準，回錯的基準則會讓整段區間偏移。

把第 1 種寫成第 3 種，子類會靜靜沿用一個錯的預設；把第 3 種寫成第 1 種，
每個子類都被迫寫一份 `pass`，而那等於把「這裡本來就沒事要做」這個資訊丟掉。
"""


class BaseDataFeed(ABC):
    """
    資料源：把某個市場的原始資料變成引擎看得懂的報價

    不算「行為 model」，但必須一起抽——否則引擎的資料載入仍會直接 import
    各（市場, 商品）組合的具體 API，`Backtester` 就不可能與兩者無關。
    """

    @abstractmethod
    def setup(self, strategy: BaseStrategy) -> None:
        """
        - Description:
            依策略宣告的級別建立所需的資料 API
        - Parameters:
            - strategy: BaseStrategy
                本次回測的策略
        """
        pass

    @abstractmethod
    def is_market_open(self, date: datetime.date) -> bool:
        """
        - Description:
            判斷指定日期是否為該市場的交易日
        - Parameters:
            - date: datetime.date
                待判定的日期
        - Return:
            - bool
        """
        pass

    def close(self) -> None:
        """關閉所有資料連線；預設為 no-op，有連線的資料源自行覆寫"""

        pass

    def get_price_limit_basis(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日「漲跌停基準價」與前一交易日收盤不同的標的

            一般日子的基準就是前收盤，由 `FillModel` 自行累積即可；
            但除權息日的基準是交易所另行公告的**開盤競價基準**，沿用前收會讓
            整段漲跌停區間偏移。有這類公告的市場覆寫本方法即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 基準價}`；沒有這種公告的市場回傳空 dict（預設）
        """

        return {}

    def get_short_balance(self, date: datetime.date) -> Dict[str, int]:
        """
        - Description:
            取得當日可放空的券源餘額

            台股為融券今日餘額（張）。**空 dict 代表「查無資料」而非「都借不到」**，
            `FillModel` 查不到時一律放行。沒有券源概念的市場沿用預設即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, int]
                `{symbol: 可借券張數}`；預設為空 dict
        """

        return {}

    def get_force_cover_symbols(self, date: datetime.date) -> Set[str]:
        """
        - Description:
            取得當日觸發「停券強制回補」的標的

            台股為除權息前的融券最後回補日。**空集合代表「今日沒有標的停券」**，
            與券源餘額不同，這裡查不到就是真的沒有——停券日由行事曆推導，
            不存在「資料缺一天」的中間狀態。沒有停券制度的市場沿用預設即可。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Set[str]
                `{symbol}`；預設為空集合
        """

        return set()

    def get_short_suspended_symbols(self, date: datetime.date) -> Set[str]:
        """
        - Description:
            取得今日處於**停券期間**的標的

            與 `get_force_cover_symbols()` 的差別是「一天」與「一段」：後者是
            融券最後回補日當天，本方法涵蓋從那天起到除權息交易日之間的整段期間，
            這段期間制度上不得新增融券賣出。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Set[str]
                `{symbol}`；預設為空集合
        """

        return set()

    def get_cash_dividend_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日除息的每股現金股利

            供放空的股利補償計算使用（放空者須補償出借方當期股利）。
            值可能為 `NaN`——代表「有除權息但無法拆出現金股利」，
            與 key 不存在（當日未除息）語意不同，呼叫端不可一律當成 0。
        - Parameters:
            - date: datetime.date
                除權息交易日
        - Return:
            - Dict[str, float]
                `{symbol: 每股現金股利}`；預設為空 dict
        """

        return {}

    def get_share_ratio_map(self, date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            取得當日的股數倍率（`新股數 / 舊股數`）

            配股、分割、減資會改變手上的股數與每股成本，而價格序列同時跳動。
            記帳端不跟著調整的話，張數不變、價格砍半，帳面就憑空虧一半——
            那與訊號面的還原價是兩回事，兩者都要做。
        - Parameters:
            - date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{symbol: 股數倍率}`；預設為空 dict（沒有這種制度的市場）
        """

        return {}

    @abstractmethod
    def get_quotes(
        self,
        date: datetime.date,
        scale: Scale,
        adjusted: bool = False,
    ) -> List[BaseQuote]:
        """
        - Description:
            取得指定日期、指定級別的報價
        - Parameters:
            - date: datetime.date
                交易日
            - scale: Scale
                報價級別（DAY / TICK）
            - adjusted: bool
                是否附上還原價（掛在 `BaseQuote.adj_close`，OHLC 一律維持原始價）。
                不支援還原的市場忽略此參數即可
        - Return:
            - List[BaseQuote]
                該日報價；無資料時回傳空 list
        """
        pass
