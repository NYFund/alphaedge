import csv
import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from loguru import logger

from core.config.paths import LIVE_RESULT_DIR_PATH
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.models import BaseOrder
from core.utils import LiveOrderStatus

"""
訊號 parity：同一支策略在回測與實盤有沒有送出同一批委託

**這是整份實盤規劃的核心假設，而它不主動比對就看不出來**——策略少送一張單不會有
任何錯誤訊息，回測績效卻已經失去參考價值。滑價與成本可以事後校正，訊號漂移不行。

比對的是**委託**不是成交：成不成交是市場的事，「這張單有沒有被送出去」才是策略層
的責任。故兩邊取的都是「通過方向白名單、檔數上限與排序之後、進入成交／送單之前」
的那一份。

**每一筆差異都必須歸到一個類別**。`UNEXPLAINED` 是唯一會推播 CRITICAL 的類別，
它的意義是「我們還不知道為什麼」——把已知的制度性差異（快照口徑、跨策略守門、
資金排擠）混進去，真正的未解釋差異就會被雜訊淹沒。
"""

# 差異類別。**順序即判定優先級**：一筆差異可能同時符合多條，取第一條命中的
CATEGORY_SNAPSHOT_GAP: str = "SNAPSHOT_GAP"
CATEGORY_RISK_REJECTED: str = "RISK_REJECTED"
CATEGORY_UNFILLED: str = "UNFILLED"
CATEGORY_TIMING: str = "TIMING"
CATEGORY_CROSS_STRATEGY_BLOCKED: str = "CROSS_STRATEGY_BLOCKED"
CATEGORY_CAPITAL_EXHAUSTED: str = "CAPITAL_EXHAUSTED"
CATEGORY_UNEXPLAINED: str = "UNEXPLAINED"

# 風控與守門寫進 `live_risk_event` 的類別 → parity 類別。
# **以事件為準而不是猜**：哪一張單被誰擋下來，當下就已經寫進紀錄庫了
RISK_EVENT_TO_CATEGORY: Dict[str, str] = {
    "CROSS_STRATEGY_CONFLICT": CATEGORY_CROSS_STRATEGY_BLOCKED,
    "SAME_SYMBOL_HELD": CATEGORY_CROSS_STRATEGY_BLOCKED,
    "CAPITAL_RESERVE_FAILED": CATEGORY_CAPITAL_EXHAUSTED,
    "MAX_HOLDINGS": CATEGORY_RISK_REJECTED,
    "DAILY_LOSS": CATEGORY_RISK_REJECTED,
    "ACCOUNT_DAILY_LOSS": CATEGORY_RISK_REJECTED,
    "DEGRADE": CATEGORY_RISK_REJECTED,
}

PARITY_COLUMNS: List[str] = [
    "seq",
    "symbol",
    "side",
    "category",
    "live_detail",
    "backtest_detail",
    "note",
]


@dataclass(frozen=True)
class ParityDiff:
    """一筆回測與實盤的委託差異"""

    seq: int
    symbol: str
    side: str
    category: str
    live_detail: str
    backtest_detail: str
    note: str

    @property
    def is_unexplained(self) -> bool:
        """未歸因的差異；這是唯一要推播 CRITICAL 的類別"""

        return self.category == CATEGORY_UNEXPLAINED


def order_key(symbol: str, action: str) -> Tuple[str, str]:
    """
    比對鍵：標的 ＋ 買賣別

    **刻意不含數量與價格**：數量差異屬於同一張單的「部分差異」，
    用它當鍵會讓一張單變成「回測少一張、實盤多一張」兩筆差異，
    真正的問題（張數算錯）反而看不出來。
    """

    return (str(symbol), str(action))


def summarize(order: BaseOrder) -> str:
    """把一張回測委託壓成一行可讀的摘要"""

    return f"{order.symbol} {order.action.value} {order.volume} @ {order.price}"


def summarize_row(row: Dict[str, Any]) -> str:
    """把一筆 `live_order` 壓成一行可讀的摘要"""

    return (
        f"{row.get('symbol')} {row.get('action')} {row.get('volume')} "
        f"@ {row.get('price')}（{row.get('status')}）"
    )


class ParityChecker:
    """
    - Description:
        比對當日實盤委託與「同一支策略跑同一天回測」會送出的委託

        **回測那一半由外部注入**：跑回測要用 `core.backtest.factory`（組裝層），
        而本模組在元件層——自己 import 它會造成反向相依。注入之後，
        測試也不必為了比對邏輯真的跑一場回測。
    """

    def __init__(
        self,
        dao: LiveTradeDAO,
        run_backtest: Callable[[str, datetime.date], List[BaseOrder]],
        output_root: Path = LIVE_RESULT_DIR_PATH,
    ) -> None:
        """
        - Description:
            建立 parity 比對器
        - Parameters:
            - dao: LiveTradeDAO
                實盤紀錄庫
            - run_backtest: Callable[[str, datetime.date], List[BaseOrder]]
                `(策略名, 交易日) → 該日回測會送出的委託清單`
            - output_root: Path
                CSV 輸出根目錄；**預設 `results/live/`，不可指向回測的結果目錄**——
                那裡是回歸雙線的比對基準，被每日 parity 的單日結果覆蓋掉，
                `run_regression.sh` 就失去意義了
        """

        self.dao: LiveTradeDAO = dao
        self.run_backtest: Callable[[str, datetime.date], List[BaseOrder]] = (
            run_backtest
        )
        self.output_root: Path = output_root

    def check(self, run_date: datetime.date) -> Dict[str, List[ParityDiff]]:
        """
        - Description:
            逐策略比對當日委託，結果寫 `live_parity_diff` 與 CSV
        - Parameters:
            - run_date: datetime.date
                交易日
        - Return:
            - Dict[str, List[ParityDiff]]
                `{策略名: 差異清單}`；沒有差異的策略也會出現（值為空 list）
        """

        live_orders: List[Dict[str, Any]] = self.dao.get_orders_by_date(run_date)
        events: List[Dict[str, Any]] = self.dao.get_risk_events_by_date(run_date)

        result: Dict[str, List[ParityDiff]] = {}
        for name in sorted({str(row["strategy_name"]) for row in live_orders}):
            diffs: List[ParityDiff] = self._check_strategy(
                name,
                run_date,
                [row for row in live_orders if row["strategy_name"] == name],
                [event for event in events if event.get("strategy_name") == name],
            )
            result[name] = diffs
            self._persist(name, run_date, diffs)

        return result

    def _check_strategy(
        self,
        strategy_name: str,
        run_date: datetime.date,
        live_orders: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
    ) -> List[ParityDiff]:
        """比對單一策略；回測跑不起來時視為整批未解釋，不是靜靜跳過"""

        try:
            backtest_orders: List[BaseOrder] = self.run_backtest(
                strategy_name, run_date
            )
        except Exception as exc:
            logger.opt(exception=True).error(
                f"{strategy_name} 的當日回測跑不起來，parity 無法比對：{exc}"
            )
            return [
                ParityDiff(
                    seq=1,
                    symbol="",
                    side="",
                    category=CATEGORY_UNEXPLAINED,
                    live_detail=f"實盤 {len(live_orders)} 筆委託",
                    backtest_detail="回測未能執行",
                    note=str(exc),
                )
            ]

        return compare(live_orders, backtest_orders, events)

    def _persist(
        self, strategy_name: str, run_date: datetime.date, diffs: List[ParityDiff]
    ) -> Optional[Path]:
        """寫入紀錄庫與 CSV；**沒有差異也要寫一份空的 CSV**"""

        for diff in diffs:
            self.dao.upsert_parity_diff(
                {
                    "date": run_date,
                    "strategy_name": strategy_name,
                    "seq": diff.seq,
                    "symbol": diff.symbol,
                    "side": diff.side,
                    "category": diff.category,
                    "live_detail": diff.live_detail,
                    "backtest_detail": diff.backtest_detail,
                    "note": diff.note,
                }
            )
        self.dao.commit()

        directory: Path = self.output_root / strategy_name
        directory.mkdir(parents=True, exist_ok=True)
        path: Path = directory / f"{run_date.isoformat()}_parity.csv"

        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(PARITY_COLUMNS)
            for diff in diffs:
                writer.writerow(
                    [
                        diff.seq,
                        diff.symbol,
                        diff.side,
                        diff.category,
                        diff.live_detail,
                        diff.backtest_detail,
                        diff.note,
                    ]
                )
        return path


def compare(
    live_orders: Sequence[Dict[str, Any]],
    backtest_orders: Sequence[BaseOrder],
    events: Sequence[Dict[str, Any]],
) -> List[ParityDiff]:
    """
    - Description:
        比對兩邊的委託並逐筆歸因

        **純函式**：不碰資料庫、不碰檔案，連實例都不必建就測得動。
        風控與守門的歸因一律以 `live_risk_event` 為準，不用猜的——
        哪一張單被誰擋下來，當下就已經寫進紀錄庫了。
    - Parameters:
        - live_orders: Sequence[Dict[str, Any]]
            當日 `live_order` 的列
        - backtest_orders: Sequence[BaseOrder]
            同一天回測會送出的委託
        - events: Sequence[Dict[str, Any]]
            當日 `live_risk_event` 的列；用來歸因「實盤沒送出去」的那些
    - Return:
        - List[ParityDiff]
            差異清單；兩邊完全一致時為空
    """

    live_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in live_orders:
        live_by_key.setdefault(order_key(row["symbol"], row["action"]), []).append(row)

    backtest_by_key: Dict[Tuple[str, str], List[BaseOrder]] = {}
    for order in backtest_orders:
        backtest_by_key.setdefault(
            order_key(order.symbol, order.action.value), []
        ).append(order)

    blocked: Dict[str, str] = _blocked_symbols(events)

    diffs: List[ParityDiff] = []
    for key in sorted(set(live_by_key) | set(backtest_by_key)):
        symbol, side = key
        live_rows: List[Dict[str, Any]] = live_by_key.get(key, [])
        bt_orders: List[BaseOrder] = backtest_by_key.get(key, [])

        if live_rows and not bt_orders:
            diffs.append(
                _make_diff(
                    len(diffs) + 1,
                    symbol,
                    side,
                    CATEGORY_SNAPSHOT_GAP
                    if _is_threshold_gap(live_rows)
                    else CATEGORY_UNEXPLAINED,
                    "；".join(summarize_row(row) for row in live_rows),
                    "（回測沒有這張單）",
                )
            )
            continue

        if bt_orders and not live_rows:
            category: str = blocked.get(symbol, CATEGORY_UNEXPLAINED)
            diffs.append(
                _make_diff(
                    len(diffs) + 1,
                    symbol,
                    side,
                    category,
                    "（實盤沒有送出這張單）",
                    "；".join(summarize(order) for order in bt_orders),
                )
            )
            continue

        volume_diff: Optional[ParityDiff] = _compare_volume(
            len(diffs) + 1, symbol, side, live_rows, bt_orders
        )
        if volume_diff is not None:
            diffs.append(volume_diff)

    return diffs


def _blocked_symbols(events: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    """由風控事件推出「這個標的被哪一類原因擋下」"""

    blocked: Dict[str, str] = {}
    for event in events:
        category: Optional[str] = RISK_EVENT_TO_CATEGORY.get(str(event.get("category")))
        symbol: Optional[str] = event.get("symbol")
        if category is None or not symbol:
            continue
        blocked.setdefault(str(symbol), category)
    return blocked


def _is_threshold_gap(live_rows: Sequence[Dict[str, Any]]) -> bool:
    """
    實盤多送的單能不能歸給快照口徑

    13:20 的快照不含收盤集合競價，成交量與收盤價都還不是最終值，門檻型訊號
    因此可能在實盤成立、在回測（拿最終收盤資料）不成立。**只有開倉單適用**：
    平倉的依據是持倉而不是門檻，多送一張平倉單不是口徑問題。
    """

    return all(
        str(row.get("position_type", "")) and _is_opening(row) for row in live_rows
    )


def _is_opening(row: Dict[str, Any]) -> bool:
    """這筆 `live_order` 是不是開倉單"""

    action: str = str(row.get("action", ""))
    position_type: str = str(row.get("position_type", ""))
    return (position_type == "LONG" and action == "Buy") or (
        position_type == "SHORT" and action == "Sell"
    )


def _compare_volume(
    seq: int,
    symbol: str,
    side: str,
    live_rows: Sequence[Dict[str, Any]],
    bt_orders: Sequence[BaseOrder],
) -> Optional[ParityDiff]:
    """兩邊都有這張單時，比數量；相同則不算差異"""

    live_volume: int = sum(int(row.get("volume", 0)) for row in live_rows)
    bt_volume: int = sum(int(order.volume) for order in bt_orders)
    if live_volume == bt_volume:
        return None

    rejected: bool = all(
        str(row.get("status")) == LiveOrderStatus.REJECTED.value for row in live_rows
    )
    return _make_diff(
        seq,
        symbol,
        side,
        CATEGORY_RISK_REJECTED if rejected else CATEGORY_UNEXPLAINED,
        "；".join(summarize_row(row) for row in live_rows),
        "；".join(summarize(order) for order in bt_orders),
        note=f"數量不同：實盤 {live_volume}、回測 {bt_volume}",
    )


def _make_diff(
    seq: int,
    symbol: str,
    side: str,
    category: str,
    live_detail: str,
    backtest_detail: str,
    note: str = "",
) -> ParityDiff:
    """建一筆差異並在未解釋時留下明顯的 log"""

    diff: ParityDiff = ParityDiff(
        seq=seq,
        symbol=symbol,
        side=side,
        category=category,
        live_detail=live_detail,
        backtest_detail=backtest_detail,
        note=note,
    )
    if diff.is_unexplained:
        logger.error(
            f"[Parity] {symbol} {side} 未解釋的差異："
            f"實盤={live_detail}、回測={backtest_detail}"
        )
    return diff
