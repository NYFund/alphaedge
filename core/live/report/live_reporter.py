import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from loguru import logger

from core.config import LIVE_RESULT_DIR_PATH
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.utils import Action, FileEncoding

"""
LiveReporter：每天收盤後留下可稽核的三份 CSV

**欄位名與回測報表對齊**（Title Case ＋ 空白，例如 `Symbol`、`Position Type`、
`Realized PnL`）：前端與分析腳本之後可以沿用同一套欄位，
而 parity 比對要把兩邊的委託逐筆對起來時，欄位名不同就得先寫一層對照表。

**輸出一律寫到 `results/live/` 底下，不可寫進回測結果根目錄**：那裡是回歸雙線的
比對基準，被每日的實盤結果覆蓋掉，`run_regression.sh` 就失去意義了——
而且是安靜地失去。
"""

# 委託報表欄位；`Slippage` 由成交均價與委託價算出
ORDER_COLUMNS: List[str] = [
    "Client Order ID",
    "Strategy",
    "Symbol",
    "Action",
    "Position Type",
    "Order Price",
    "Order Volume",
    "Price Type",
    "Timing",
    "Status",
    "Filled Volume",
    "Avg Fill Price",
    "Slippage",
    "Reject Reason",
    "Dry Run",
    "Created At",
]

FILL_COLUMNS: List[str] = [
    "Broker Seqno",
    "Broker Trade ID",
    "Client Order ID",
    "Strategy",
    "Symbol",
    "Action",
    "Fill Price",
    "Fill Volume",
    "Estimated Commission",
    "Estimated Tax",
    "Commission",
    "Tax",
    "Filled At",
]

POSITION_COLUMNS: List[str] = [
    "Strategy",
    "Symbol",
    "Position Type",
    "Volume",
    "Avg Price",
    "Order Cond",
    "Unrealized PnL",
    "Source",
]


class LiveReporter:
    """實盤每日報表"""

    def __init__(
        self,
        dao: LiveTradeDAO,
        output_root: Path = LIVE_RESULT_DIR_PATH,
    ) -> None:
        """
        - Description:
            建立報表產生器
        - Parameters:
            - dao: LiveTradeDAO
                實盤紀錄庫
            - output_root: Path
                輸出根目錄；預設 `results/live/`
        """

        self.dao: LiveTradeDAO = dao
        self.output_root: Path = output_root

    def write_daily_reports(self, run_date: datetime.date) -> Dict[str, Path]:
        """
        - Description:
            輸出當日三份 CSV，逐策略各一個目錄

            **筆數要和資料庫一致**：報表是給人看的，但它同時是事後追查的入口；
            少一筆就等於那筆交易在追查時不存在。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - Dict[str, Path]
                `{"<策略>/orders": 路徑, ...}`
        """

        orders: List[Dict[str, Any]] = self.dao.get_orders_by_date(run_date)
        fills: List[Dict[str, Any]] = self.dao.get_fills_by_date(run_date)
        positions: List[Dict[str, Any]] = self.dao.get_position_snapshots(run_date)

        fill_price_map: Dict[str, float] = self._average_fill_prices(fills)
        written: Dict[str, Path] = {}

        for strategy in self._strategy_names(orders, fills, positions):
            directory: Path = self.output_root / strategy
            stamp: str = run_date.isoformat()

            written[f"{strategy}/orders"] = self._write(
                directory / f"{stamp}_orders.csv",
                ORDER_COLUMNS,
                [
                    self._order_row(order, fill_price_map)
                    for order in orders
                    if order.get("strategy_name") == strategy
                ],
            )
            written[f"{strategy}/fills"] = self._write(
                directory / f"{stamp}_fills.csv",
                FILL_COLUMNS,
                [
                    self._fill_row(fill)
                    for fill in fills
                    if fill.get("strategy_name") == strategy
                ],
            )
            written[f"{strategy}/positions"] = self._write(
                directory / f"{stamp}_positions.csv",
                POSITION_COLUMNS,
                [
                    self._position_row(position)
                    for position in positions
                    if position.get("strategy_name") == strategy
                ],
            )

        return written

    # === 滑價 ===
    def summarize_slippage(self, run_date: datetime.date) -> Dict[str, float]:
        """
        - Description:
            逐策略彙總當日實際滑價（成交均價 − 委託價，依方向取號）

            這是之後校正回測成交假設的依據。**不另外開欄位存**：
            `live_order.price` 與 `live_fill.price` 都已經落地，滑價是它們的差，
            多存一份就多一個會漂移的地方。
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - Dict[str, float]
                `{策略: 平均滑價}`；當日沒有成交的策略不會出現
        """

        orders: List[Dict[str, Any]] = self.dao.get_orders_by_date(run_date)
        totals: Dict[str, List[float]] = {}

        for order in orders:
            slippage: Optional[float] = self._slippage(order)
            if slippage is None:
                continue
            totals.setdefault(str(order["strategy_name"]), []).append(slippage)

        summary: Dict[str, float] = {
            name: sum(values) / len(values) for name, values in totals.items() if values
        }
        for name, value in summary.items():
            logger.info(f"[{name}] 當日平均滑價 {value:+.4f}")
        return summary

    @staticmethod
    def _slippage(order: Dict[str, Any]) -> Optional[float]:
        """
        單張委託的滑價；未成交或市價單回 None

        **市價單沒有委託價可比**（送出時價格是 0），把它算進來會得到一個等於
        成交價的巨大滑價，把整個統計拉歪。
        """

        filled: int = int(order.get("filled_volume") or 0)
        order_price: float = float(order.get("price") or 0.0)
        fill_price: float = float(order.get("avg_fill_price") or 0.0)
        if filled <= 0 or order_price <= 0 or fill_price <= 0:
            return None

        # 買進成交價較高 ⇒ 滑價為正（不利）；賣出相反
        sign: int = 1 if order.get("action") == Action.BUY.value else -1
        return (fill_price - order_price) * sign

    # === 內部 ===
    @staticmethod
    def _average_fill_prices(fills: List[Dict[str, Any]]) -> Dict[str, float]:
        """以成交量加權算出各委託的成交均價"""

        totals: Dict[str, List[float]] = {}
        for fill in fills:
            client_order_id: Optional[str] = fill.get("client_order_id")
            if not client_order_id:
                continue
            totals.setdefault(str(client_order_id), []).append(
                float(fill["price"]) * float(fill["volume"])
            )

        volumes: Dict[str, float] = {}
        for fill in fills:
            client_order_id = fill.get("client_order_id")
            if not client_order_id:
                continue
            volumes[str(client_order_id)] = volumes.get(
                str(client_order_id), 0.0
            ) + float(fill["volume"])

        return {
            key: sum(values) / volumes[key]
            for key, values in totals.items()
            if volumes.get(key)
        }

    @staticmethod
    def _strategy_names(*collections: List[Dict[str, Any]]) -> List[str]:
        """出現在任一份資料裡的策略名"""

        names: set = set()
        for collection in collections:
            names |= {
                str(row["strategy_name"])
                for row in collection
                if row.get("strategy_name")
            }
        return sorted(names)

    def _order_row(
        self, order: Dict[str, Any], fill_price_map: Dict[str, float]
    ) -> List[Any]:
        """委託列；成交均價優先取成交明細算出來的值"""

        client_order_id: str = str(order["client_order_id"])
        avg_price: float = fill_price_map.get(
            client_order_id, float(order.get("avg_fill_price") or 0.0)
        )
        enriched: Dict[str, Any] = {**order, "avg_fill_price": avg_price}

        return [
            client_order_id,
            order.get("strategy_name"),
            order.get("symbol"),
            order.get("action"),
            order.get("position_type"),
            order.get("price"),
            order.get("volume"),
            order.get("price_type"),
            order.get("timing"),
            order.get("status"),
            order.get("filled_volume"),
            avg_price or None,
            self._slippage(enriched),
            order.get("reject_reason"),
            bool(order.get("dry_run")),
            order.get("created_at"),
        ]

    @staticmethod
    def _fill_row(fill: Dict[str, Any]) -> List[Any]:
        """成交列；估算成本與券商實際值分欄"""

        return [
            fill.get("broker_seqno"),
            fill.get("broker_trade_id"),
            fill.get("client_order_id"),
            fill.get("strategy_name"),
            fill.get("symbol"),
            fill.get("action"),
            fill.get("price"),
            fill.get("volume"),
            fill.get("estimated_fee"),
            fill.get("estimated_tax"),
            fill.get("actual_fee"),
            fill.get("actual_tax"),
            fill.get("filled_at"),
        ]

    @staticmethod
    def _position_row(position: Dict[str, Any]) -> List[Any]:
        """部位列"""

        return [
            position.get("strategy_name"),
            position.get("symbol"),
            position.get("direction"),
            position.get("volume"),
            position.get("avg_price"),
            position.get("order_cond"),
            position.get("unrealized_pnl"),
            position.get("source"),
        ]

    @staticmethod
    def _write(path: Path, columns: List[str], rows: List[List[Any]]) -> Path:
        """
        寫出一份 CSV

        **沒有資料也要寫出空表**：檔案不存在與「今天沒有交易」是兩回事，
        而盤後檢查只看得到檔案在不在。
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        frame: pd.DataFrame = pd.DataFrame(rows, columns=columns)
        frame.to_csv(path, index=False, encoding=FileEncoding.UTF8_SIG.value)
        logger.info(f"* 實盤報表已輸出：{path}（{len(rows)} 列）")
        return path
