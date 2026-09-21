import shioaji as sj
from shioaji import Contract, Order


class OrderUtils:
    """執行買入、賣出等下單操作"""

    @staticmethod
    # place buy or sell order
    def place_order(api: sj.Shioaji, order: dict) -> None:
        """
        下單操作
        order 格式
        order = {
            'code': '2330',
            'volume': 3,
            'price': 0,                         # 市價單價格為0
            'price_type': StockPriceType.MKT,   # 市價單 or 限價單
            'action': Action.BUY,               # 買入 or 賣出
            'order_type': OrderType.IOC,        # 下單類型
            'order_lot': StockOrderLot.Common,  # 下單單位
        }
        """

        # 商品檔
        contract: Contract = api.Contracts.Stocks.TSE.get(
            order["code"]
        ) or api.Contracts.Stocks.OTC.get(order["code"])

        # 委託單
        order: Order = api.Order(
            price=order["price"],
            quantity=order["volume"],
            action=order["action"],
            price_type=order["price_type"],
            order_type=order["order_type"],
            order_lot=order["order_lot"],
            # daytrade_short = False,
            # custom_field="test",
            account=api.stock_account,
        )

        api.place_order(contract, order)
