import os
from typing import List, Optional, Union

import numpy as np
import pandas as pd
import shioaji as sj
from shioaji import FutureProfitLoss, StockProfitLoss, Trade

from .constant import Action, Status


class ShioajiAccount:
    """Shioaji API Account Tool"""

    # API login
    @staticmethod
    def API_login(
        api: sj.Shioaji,
        api_key: str,
        api_secret_key: str,
    ) -> Optional[sj.Shioaji]:
        """API 登入"""

        try:
            print("API logging in...")
            api.login(api_key=api_key, secret_key=api_secret_key)
            print("API logging in successfully!")
            return api
        except Exception as e:
            print(f"API logging failed: {e}")
            return None

    # API logout
    @staticmethod
    def API_logout(api: sj.Shioaji) -> None:
        """API 登出"""

        print("API logging out...")
        api.logout()
        print("API logging out successfully!")

    # Calculate capital after T+2
    @staticmethod
    def get_settlement_capital(api: sj.Shioaji) -> float:
        """計算 T+2 後的本金（帳戶可用的總資金）"""

        balance: float = api.account_balance().acc_balance
        settlements: pd.DataFrame = pd.DataFrame(
            [s.dict() for s in api.settlements(api.stock_account)]
        )
        total_capital: float = balance + settlements.loc[1:2, "amount"].sum()
        return total_capital

    @staticmethod
    def get_invest_capital(api: sj.Shioaji, capital: float) -> float:
        """計算預計投入的資金"""

        total_capital: float = ShioajiAccount.get_settlement_capital(
            api
        )  # 帳戶可用的總資金
        settlements: pd.DataFrame = pd.DataFrame(
            [s.dict() for s in api.settlements(api.stock_account)]
        )
        capital_quota: float = (
            capital + settlements.loc[2, "amount"]
        )  # 目前剩餘的投資額度
        invest_capital: float = (
            total_capital if total_capital <= capital_quota else capital_quota
        )
        return invest_capital

    @staticmethod
    def get_realized_pnl(api: sj.Shioaji) -> int:
        """計算已實現損益"""

        pnl_df: pd.DataFrame = pd.DataFrame(
            s.dict() for s in api.list_profit_loss(api.stock_account)
        )
        realized_pnl: int = round(pnl_df["pnl"].sum()) if len(pnl_df) > 0 else 0
        return realized_pnl

    @staticmethod
    def get_unrealized_pnl(api: sj.Shioaji) -> int:
        """計算未實現損益"""

        positions_df: pd.DataFrame = pd.DataFrame(
            s.dict() for s in api.list_positions(api.stock_account)
        )
        unrealized_pnl: int = (
            round(positions_df["pnl"].sum()) if len(positions_df) > 0 else 0
        )
        return unrealized_pnl

    @staticmethod
    def generate_trade_record(api: sj.Shioaji, trade_record_path: str) -> None:
        """製作投資紀錄表（模擬模式）"""

        columns_name: List[str] = [
            "date",
            "stock_id",
            "order_action",
            "seqno",
            "volume",
            "buy_price",
            "sell_price",
            "pnl",
        ]
        trade_record_df: pd.DataFrame = pd.DataFrame(columns=columns_name)

        api.update_status(api.stock_account)
        trade_list: List[Trade] = api.list_trades()
        pnl_list: List[Union[StockProfitLoss, FutureProfitLoss]] = (
            api.list_profit_loss()
        )

        # Create trading record
        for trade in trade_list:
            if trade.status.status == Status.Filled:
                # 計算平均成交價
                total_value: float = sum(
                    deal.price * deal.quantity for deal in trade.status.deals
                )
                ave_deal_price: float = round(
                    total_value / trade.order.quantity,
                    2,
                )

                trade_record: pd.DataFrame = pd.DataFrame(
                    [
                        [
                            trade.status.order_datetime,
                            trade.contract.code,
                            trade.order.action,
                            trade.order.seqno,
                            trade.order.quantity,
                            (
                                ave_deal_price
                                if trade.order.action == Action.BUY
                                else np.nan
                            ),
                            (
                                ave_deal_price
                                if trade.order.action == Action.SELL
                                else np.nan
                            ),
                            0,
                        ]
                    ],
                    columns=columns_name,
                )
                trade_record_df = pd.concat(
                    [trade_record_df, trade_record], ignore_index=True
                )

        # open existed record
        exist_trade_record: pd.DataFrame = (
            pd.read_csv(trade_record_path)
            if os.path.exists(trade_record_path)
            else pd.DataFrame(columns=columns_name)
        )
        exist_trade_record = pd.concat(
            [exist_trade_record, trade_record_df], ignore_index=True
        )

        # Add pnl to trading record
        for trade in pnl_list:
            exist_trade_record.loc[
                exist_trade_record["seqno"] == trade.seqno, "pnl"
            ] = trade.pnl

        # Store the record to csv file
        exist_trade_record.to_csv(trade_record_path, index=False)


class ShioajiAPI:
    """Shioaji API_KEY and API_SECRET_KEY"""

    def __init__(self, api_key: str, api_secret_key: str) -> None:
        self.api_key: str = api_key
        self.api_secret_key: str = api_secret_key
