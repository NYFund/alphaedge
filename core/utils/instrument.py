from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import List, Tuple

from .constant import PRICE_TICK_TABLE, Commission, Units

"""
instrument.py

Utility functions for asset trading calculations, including support for stocks, futures, and options.

Features:
- Calculate commission, tax, net profit, and ROI
- 跳動點與漲跌停價的換算

Designed for use in backtesting and trading performance analysis
"""


class StockUtils:
    """Stock Related Tools"""

    @staticmethod
    def convert_share_to_lot(shares: int) -> int:
        """
        - Description: 將股數轉換為張數
        - Parameters:
            - shares: int
                股數 (Unit: Shares)
        - Return:
            - lots: int
                張數 (Unit: Lots)
        """

        return int(shares / Units.LOT)

    @staticmethod
    def convert_lot_to_share(lots: int) -> int:
        """
        - Description: 將張數轉換為股數
        - Parameters:
            - lots: int
                張數 (Unit: Lots)
        - Return:
            - shares: int
                股數 (Unit: Shares)
        """

        return int(lots * Units.LOT)

    @staticmethod
    def calculate_transaction_commission(price: float = 0.0, volume: int = 0) -> int:
        """
        - Description:
            計算股票買賣時的手續費

            ⚠️ **記帳的唯一入口是 `StockCostModel.commission()`**，不要從
            `core/managers/`、`core/backtest/` 或策略層直接呼叫本函式。

            **兩者尚未收斂**：`StockCostModel.commission()` 自己從 `self.config`
            推算，一次都沒有呼叫過本函式。現行呼叫端只有同檔的
            `calculate_transaction_cost()`、`strategy_lab/` 的研究腳本與測試，
            所以口徑差異目前不影響任何回測數字；但同一條規則存在兩份，
            改費率時只改一邊就會分岔。
        - Parameters:
            - price: float
                成交價格
            - volume: int
                成交張數（Unit: Lots）
        - Return:
            - commission: int
                手續費
        - Notes:
            做多部位的手續費由買賣兩端各收一次：
            - 券買手續費 = 成交價 x 成交股數 x 手續費率 x discount
            - 券賣手續費 = 成交價 x 成交股數 x 手續費率 x discount
        """

        return max(
            Commission.MinFee,
            int(
                price
                * StockUtils.convert_lot_to_share(volume)
                * Commission.CommRate
                * Commission.Discount
            ),
        )

    @staticmethod
    def calculate_transaction_tax(
        price: float = 0.0,
        volume: int = 0,
        is_day_trade: bool = False,
    ) -> int:
        """
        - Description:
            計算股票賣出時的交易稅

            ⚠️ **記帳的唯一入口是 `StockCostModel.tax()`**，不要從 `core/managers/`、
            `core/backtest/` 或策略層直接呼叫本函式。

            **兩者尚未收斂，而且口徑不同**：`StockCostModel.tax()` 自己從
            `self.config` 推算，並會**依成交日**判斷當沖減半是否已實施；本函式吃
            模組層級常數、不看日期，`is_day_trade=True` 就一律減半。它一次都沒有
            被 `StockCostModel` 呼叫過，現行呼叫端只有同檔的
            `calculate_transaction_cost()`、`strategy_lab/` 的研究腳本與測試，
            所以差異目前不影響任何回測數字；但同一條規則存在兩份，
            改法規時只改一邊就會分岔。
        - Parameters:
            - price: float
                成交價格
            - volume: int
                成交張數（Unit: Lots）
            - is_day_trade: bool
                是否為現股當沖賣出（稅率減半），預設 False 維持既有行為
        - Return:
            - tax: int
                交易稅
        - Notes:
            - 一般賣出證交稅 = 成交價 x 成交股數 x 0.3%
            - 現股當沖賣出證交稅 = 成交價 x 成交股數 x 0.15%（減半優惠）
            - 放空的證交稅課在「賣出（開倉）」這端，與做多相反
        """

        tax_rate: float = (
            Commission.DayTradeTaxRate if is_day_trade else Commission.TaxRate
        )
        return max(1, int(price * StockUtils.convert_lot_to_share(volume) * tax_rate))

    @staticmethod
    def round_to_tick(price: float, direction: str = "nearest") -> float:
        """
        - Description:
            將價格對齊台股分段檔位，避免算出不可能成交的價格
        - Parameters:
            - price: float
                原始價格（例如滑價調整後的價格）
            - direction: str
                取整方向："up"（進位）、"down"（捨去）、"nearest"（就近）
                放空情境建議：開倉（賣出）用 "down"、回補（買進）用 "up"，較為保守
        - Return:
            - price: float
                對齊檔位後的價格
        """

        if price <= 0:
            return 0.0

        # 找出該價位適用的檔位；邊界值（如 10、50）歸屬較大的檔位級距
        tick: float = PRICE_TICK_TABLE[-1][1]
        for upper_bound, tick_size in PRICE_TICK_TABLE:
            if price < upper_bound:
                tick = tick_size
                break

        # 以 Decimal 運算避免浮點誤差（例如 0.05 檔位在二進位下無法精確表示）
        price_dec: Decimal = Decimal(str(price))
        tick_dec: Decimal = Decimal(str(tick))
        units: Decimal = price_dec / tick_dec

        if direction == "up":
            rounded: Decimal = units.to_integral_value(rounding=ROUND_CEILING)
        elif direction == "down":
            rounded = units.to_integral_value(rounding=ROUND_FLOOR)
        else:
            rounded = units.to_integral_value(rounding=ROUND_HALF_UP)

        return float(rounded * tick_dec)

    @staticmethod
    def calculate_transaction_cost(
        buy_price: float = 0.0,
        sell_price: float = 0.0,
        volume: int = 0,
    ) -> Tuple[int, int]:
        """
        - Description:
            計算股票買賣的手續費、交易稅等摩擦成本
        - Parameters:
            - buy_price: float
                股票買入價格
            - sell_price: float
                股票賣出價格
            - volume: int
                成交張數（Unit: Lots）
        - Return:
            - buy_transaction_cost: int
                買入交易成本
            - sell_transaction_cost: int
                賣出交易成本
        - Notes:
            做多部位的摩擦成本包含：
            - 券買手續費 = 成交價 x 成交股數 x 手續費率 x discount
            - 券賣手續費 = 成交價 x 成交股數 x 手續費率 x discount
            - 券賣證交稅 = 成交價 x 成交股數 x 證交稅率
        """

        # 買入 & 賣出的交易成本
        buy_transaction_cost: int = StockUtils.calculate_transaction_commission(
            price=buy_price, volume=volume
        )
        sell_transaction_cost: int = StockUtils.calculate_transaction_commission(
            price=sell_price, volume=volume
        ) + StockUtils.calculate_transaction_tax(price=sell_price, volume=volume)
        return (buy_transaction_cost, sell_transaction_cost)

    @staticmethod
    def calculate_net_profit(
        buy_price: float,
        sell_price: float,
        volume: int,
    ) -> float:
        """
        - Description: 計算股票交易的淨收益（扣除手續費和交易稅）（目前只有做多）

            ⚠️ **記帳的唯一入口是 `StockCostModel.realized_pnl()`**：本函式以「傳入張數」
            重算開倉手續費，部分平倉時最低手續費會被重複套用，與 `record.commission`
            的等比例攤提不一致（差異量化見 `tests/backtest/compare_cost_formula.py`）。
            生產路徑不呼叫本函式，保留僅供研究腳本比對用。
        - Parameters:
            - buy_price: float
                股票買入價格
            - sell_price: float
                股票賣出價格
            - volume: int
                成交張數（Unit: Lots）
        - Return:
            - profit: float
        """

        buy_value: float = buy_price * StockUtils.convert_lot_to_share(volume)
        sell_value: float = sell_price * StockUtils.convert_lot_to_share(volume)

        # 買入 & 賣出手續費
        buy_comm, sell_comm = StockUtils.calculate_transaction_cost(
            buy_price=buy_price,
            sell_price=sell_price,
            volume=volume,
        )

        profit: float = (sell_value - buy_value) - (buy_comm + sell_comm)
        return round(profit, 2)

    @staticmethod
    def calculate_roi(
        buy_price: float,
        sell_price: float,
        volume: int,
    ) -> float:
        """
        - Description: 計算股票投資報酬率（ROI）（目前只有做多）

            ⚠️ **記帳的唯一入口是 `StockCostModel.roi()`**，理由同 `calculate_net_profit`。
        - Parameters:
            - buy_price: float
                股票買入價格
            - sell_price: float
                股票賣出價格
            - volume: int
                成交張數（Unit: Lots）
        - Return:
            - roi: float
                投資報酬率（%）
        """

        buy_value: float = buy_price * StockUtils.convert_lot_to_share(volume)
        buy_comm, _ = StockUtils.calculate_transaction_cost(
            buy_price=buy_price,
            sell_price=sell_price,
            volume=volume,
        )

        # 計算投資成本
        investment_cost: float = buy_value + buy_comm
        if investment_cost == 0:
            return 0.0

        roi: float = (
            StockUtils.calculate_net_profit(
                buy_price=buy_price, sell_price=sell_price, volume=volume
            )
            / investment_cost
        ) * 100
        return round(roi, 2)

    @staticmethod
    def filter_common_stocks(stock_ids: List[str]) -> List[str]:
        """
        - Description:
            過濾出一般股票（排除 ETF、權證等）：保留 4 位數字且不小於 1001 的代號

            **刻意不設上限**：設了上限（例如 9958）會把 9960（邁達康）、9962（有益）
            這類真實存在的上櫃普通股靜默排除在回測股票池與券商分點更新之外。
            ETF（00 開頭）與權證（6 碼）已被「4 位數字、不小於 1001」擋掉，
            上限擋不到別的東西。
        - Parameters:
            - stock_ids: List[str]
                所有股票代號
        - Return:
            - List[str]
                符合條件的一般股票代號清單
        """

        return [
            stock_id
            for stock_id in stock_ids
            if stock_id.isdigit() and len(stock_id) == 4 and int(stock_id) >= 1001
        ]
