import csv
import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from loguru import logger

from core.config.paths import LIVE_RESULT_DIR_PATH
from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.notify.base import NotifyLevel
from core.models import BaseOrder
from core.utils import (
    Action,
    ExecutionStyle,
    LiveOrderStatus,
    OrderType,
    PositionType,
)

"""
訊號 parity：同一支策略在回測與實盤有沒有送出同一批委託

**這是整份實盤規劃的核心假設，而它不主動比對就看不出來**——策略少送一張單不會有
任何錯誤訊息，回測績效卻已經失去參考價值。滑價與成本可以事後校正，訊號漂移不行。

比對的主體是**委託**不是成交：成不成交是市場的事，「這張單有沒有被送出去」才是策略層
的責任。故兩邊取的都是「通過方向白名單、檔數上限與排序之後、進入成交／送單之前」
的那一份。兩邊都送了、實盤卻沒成交的，另外歸到執行差異的類別（見 `CATEGORY_LIMIT_UNFILLED`），
回測「一律成交」的假設偏離多少，要靠這些類別量出來。

**每一筆差異都必須歸到一個類別**。`UNEXPLAINED` 是唯一會推播 CRITICAL 的類別，
它的意義是「我們還不知道為什麼」——把已知的制度性差異（快照口徑、跨策略守門、
資金排擠）混進去，真正的未解釋差異就會被雜訊淹沒。
"""

# 差異類別。**順序即判定優先級**：一筆差異可能同時符合多條，取第一條命中的
CATEGORY_SNAPSHOT_GAP: str = "SNAPSHOT_GAP"

# 交易段落：parity 判斷「實盤當天有沒有正常跑到策略」時只看這些，
# 盤後與補比不呼叫策略鉤子，它們正不正常與訊號無關
TRADING_PHASES: Set[str] = {"open", "close", "intraday"}
CATEGORY_RISK_REJECTED: str = "RISK_REJECTED"

# 送出但未成交所造成的差異，成因是「開倉未成交一律放棄、平倉與停損未成交必須補」。
# **兩種後果都是跨日的**，故判定要有當天以外的證據：
#   1. 平倉未成交 → 次日補平單。實盤有、回測沒有（回測那天早就平掉了）。
#      證據是 `live_pending_action` 當天被處理掉的那幾筆。
#   2. 開倉未成交 → 放棄 → 實盤沒有部位 → 次日回測有平倉單、實盤沒有。
#      **這一種目前判不出來**：放棄不寫事件也不寫待辦，唯一的痕跡是前一交易日
#      委託列的 `filled_volume < volume`，而那要往前翻不定長度的歷史
CATEGORY_UNFILLED: str = "UNFILLED"

# 兩邊都送了同一張單、實盤卻沒有成交（或只成交一部分）。
# 回測一律以策略給的價成交，實盤要看市場，這是**執行差異**而不是訊號差異，
# 依執行方式分開計數，累積數據後才能決定回測要不要開始讀執行方式：
#   - `LIMIT_UNFILLED`：照價掛單（`ExecutionStyle.LIMIT`）沒等到價。回測在這裡偏樂觀。
#   - `LOCKED_AT_LIMIT`：要成交（`MARKET`）在集合競價掛到漲跌停仍排不到，
#     代表收盤鎖漲停（買）或鎖跌停（賣）。集合競價的委託效期是 ROD，以此辨識。
#   - 其餘（連續交易時段的保護價＋IOC 沒成交、沒有執行方式紀錄的舊委託）歸 `UNFILLED`。
CATEGORY_LIMIT_UNFILLED: str = "LIMIT_UNFILLED"
CATEGORY_LOCKED_AT_LIMIT: str = "LOCKED_AT_LIMIT"

# `ExecutionTiming` 造成的段落差異。
# **目前不會有任何一筆落在這一類，而那是刻意的**：開倉與平倉分屬不同段落時，
# 實際順序由段落決定、可能與 `allow_day_trade` 推導的順序相反，這個矛盾由
# `core/live/strategy_guard.py` 的 `check_schedule_conflicts()` 在 `prepare()`
# 啟動時擋掉（拋 `LiveReadinessError`），跑不到 parity 比對這一步。
# 保留這個類別是因為**檢查沒有涵蓋 `stop_loss`**——它若宣告在與 `close` 不同的段落，
# 與回測「停損 → 一般平倉」的固定順序就會分岔，而目前三支策略都沒有實作停損，
# 所以踩不到。真要收掉的做法是把 `stop_loss` 納入啟動檢查（防止），
# 而不是在這裡分類（事後解釋）
CATEGORY_TIMING: str = "TIMING"
CATEGORY_CROSS_STRATEGY_BLOCKED: str = "CROSS_STRATEGY_BLOCKED"
CATEGORY_CAPITAL_EXHAUSTED: str = "CAPITAL_EXHAUSTED"
CATEGORY_UNEXPLAINED: str = "UNEXPLAINED"

# 兩邊數量相同、實盤卻送單失敗（`FAILED`）或被券商拒單（`REJECTED`）。
# 策略做了與回測相同的決定，是執行沒做到；不歸這一類的話，2026-10-06 三張在
# 轉換層就失敗的單，parity 會判成「完全一致」
CATEGORY_SEND_FAILED: str = "SEND_FAILED"

# 類別 → 組別。parity 要回答的是三個不同的問題，處理方式完全不同：
# - 訊號差異：策略在實盤做了不同的決定，要查策略或資料。
# - 制度性差異：已知的口徑或風控造成，不是錯。
# - 執行差異：決定相同、執行沒做到，是執行成本。
# **只寫在這裡、不存進紀錄庫**：組別由類別推得出來，另存一份只會多一個可能對不上的地方
GROUP_SIGNAL: str = "訊號差異"
GROUP_STRUCTURAL: str = "制度性差異"
GROUP_EXECUTION: str = "執行差異"

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

CATEGORY_GROUP: Dict[str, str] = {
    CATEGORY_UNEXPLAINED: GROUP_SIGNAL,
    CATEGORY_TIMING: GROUP_SIGNAL,
    CATEGORY_SNAPSHOT_GAP: GROUP_STRUCTURAL,
    CATEGORY_RISK_REJECTED: GROUP_STRUCTURAL,
    CATEGORY_CROSS_STRATEGY_BLOCKED: GROUP_STRUCTURAL,
    CATEGORY_CAPITAL_EXHAUSTED: GROUP_STRUCTURAL,
    CATEGORY_UNFILLED: GROUP_EXECUTION,
    CATEGORY_LIMIT_UNFILLED: GROUP_EXECUTION,
    CATEGORY_LOCKED_AT_LIMIT: GROUP_EXECUTION,
    CATEGORY_SEND_FAILED: GROUP_EXECUTION,
}


def count_by_group(diffs: Sequence["ParityDiff"]) -> Dict[str, int]:
    """
    - Description:
        依組別計數；三組一律出現（沒有差異的組別為 0），認不得的類別歸訊號差異

        認不得的類別歸訊號差異而不是丟掉：新增類別卻忘了登記組別時，
        寧可讓它出現在最需要人看的那一組。
    - Parameters:
        - diffs: Sequence[ParityDiff]
            差異清單
    - Return:
        - Dict[str, int]
            `{組別: 筆數}`
    """

    counts: Dict[str, int] = {GROUP_SIGNAL: 0, GROUP_STRUCTURAL: 0, GROUP_EXECUTION: 0}
    for diff in diffs:
        group: str = CATEGORY_GROUP.get(diff.category, GROUP_SIGNAL)
        counts[group] += 1
    return counts


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
        strategy_names: Optional[Sequence[str]] = None,
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
            - strategy_names: Optional[Sequence[str]]
                本次行程載入的策略；只比對這些。`None` 表示比對紀錄庫裡當日有委託的全部策略
        """

        self.dao: LiveTradeDAO = dao
        self.run_backtest: Callable[[str, datetime.date], List[BaseOrder]] = (
            run_backtest
        )
        self.output_root: Path = output_root
        self.strategy_names: Optional[Set[str]] = (
            set(strategy_names) if strategy_names is not None else None
        )

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
        runs: List[Dict[str, Any]] = self.dao.get_runs_by_date(run_date)
        resolved: List[Dict[str, Any]] = self.dao.get_pending_actions_resolved_on(
            run_date
        )

        # **比對範圍＝本次行程載入的策略，不管當天有沒有委託**：
        # - 不碰別的行程的策略：股票線與期貨線是兩個行程、共用同一個紀錄庫，讀到另一個
        #   行程的委託時這裡跑不出它的回測，會記成未解釋並發 CRITICAL；差異以
        #   （日期、策略、序號）覆寫，還會蓋掉另一個行程寫好的正確結果。
        # - 當天沒送任何單的策略也要比：只比有委託的策略的話，段落中止、整天沒送單
        #   而回測會開倉的那一天完全看不到差異（2026-10-07 期貨尾盤段就是這樣）。
        # 未限定（`None`）時退回「當天有委託的策略」，供單元測試沿用
        traded: Set[str] = {str(row["strategy_name"]) for row in live_orders}
        if self.strategy_names is None:
            names: Set[str] = traded
        else:
            skipped: Set[str] = traded - self.strategy_names
            if skipped:
                logger.debug(f"略過不在本次行程的策略：{sorted(skipped)}")
            names = set(self.strategy_names)

        result: Dict[str, List[ParityDiff]] = {}
        for name in sorted(names):
            own_events: List[Dict[str, Any]] = [
                event for event in events if event.get("strategy_name") == name
            ]
            diffs: List[ParityDiff] = self._check_strategy(
                name,
                run_date,
                [row for row in live_orders if row["strategy_name"] == name],
                own_events,
                [row for row in resolved if row.get("strategy_name") == name],
                segments_ran_cleanly(name, runs, own_events),
            )
            result[name] = diffs
            self._persist(name, run_date, diffs)
            summary: str = "／".join(
                f"{group} {count}" for group, count in count_by_group(diffs).items()
            )
            logger.info(f"[Parity] {name} {run_date}：{summary}")

        return result

    def _check_strategy(
        self,
        strategy_name: str,
        run_date: datetime.date,
        live_orders: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        resolved_actions: Sequence[Dict[str, Any]] = (),
        ran_cleanly: bool = False,
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

        return compare(
            live_orders, backtest_orders, events, resolved_actions, ran_cleanly
        )

    def _persist(
        self, strategy_name: str, run_date: datetime.date, diffs: List[ParityDiff]
    ) -> Optional[Path]:
        """
        寫入紀錄庫與 CSV；**沒有差異也要寫一份空的 CSV**

        同一天可能比對不只一次（補比、手動重跑），寫之前先清掉該日該策略的舊結果。
        """

        self.dao.delete_parity_diffs(run_date, strategy_name)
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


def covered_keys(actions: Sequence[Dict[str, Any]]) -> set:
    """
    由當天處理掉的跨日待辦推出「哪些單是補前一天的」

    回傳的鍵與 `order_key()` 同形（標的 ＋ 買賣別），才能直接比對。
    """

    keys: set = set()
    for action in actions:
        symbol: Optional[str] = action.get("symbol")
        action_side: Optional[str] = action.get("action")
        if symbol and action_side:
            keys.add(order_key(str(symbol), str(action_side)))
    return keys


def compare(
    live_orders: Sequence[Dict[str, Any]],
    backtest_orders: Sequence[BaseOrder],
    events: Sequence[Dict[str, Any]],
    resolved_actions: Sequence[Dict[str, Any]] = (),
    ran_cleanly: bool = False,
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
        - resolved_actions: Sequence[Dict[str, Any]]
            當日被處理掉的 `live_pending_action` 列；用來歸因「實盤多送出去」的
            那些——次日補平單在回測沒有對應，因為回測前一天就已經平掉了
        - ran_cleanly: bool
            這支策略當天的交易段落是否都正常結束、且沒有 CRITICAL 事件
            （見 `segments_ran_cleanly()`）；決定「回測多出的開倉單」能不能歸給快照口徑
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
    covered: set = covered_keys(resolved_actions)

    diffs: List[ParityDiff] = []
    for key in sorted(set(live_by_key) | set(backtest_by_key)):
        symbol, side = key
        live_rows: List[Dict[str, Any]] = live_by_key.get(key, [])
        bt_orders: List[BaseOrder] = backtest_by_key.get(key, [])

        if live_rows and not bt_orders:
            # 優先序：快照口徑 → 次日補平 → 未解釋。
            # 兩者互斥（補平是平倉單，快照口徑只認開倉單），但仍依宣告順序判定
            if _is_threshold_gap(live_rows):
                extra_category: str = CATEGORY_SNAPSHOT_GAP
                note: str = ""
            elif key in covered:
                extra_category = CATEGORY_UNFILLED
                note = "前一交易日的平倉或停損未成交，今天依 D7 補送"
            else:
                extra_category = CATEGORY_UNEXPLAINED
                note = ""
            diffs.append(
                _make_diff(
                    len(diffs) + 1,
                    symbol,
                    side,
                    extra_category,
                    "；".join(summarize_row(row) for row in live_rows),
                    "（回測沒有這張單）",
                    note=note,
                )
            )
            continue

        if bt_orders and not live_rows:
            # 優先序：風控／守門事件 → 快照口徑（反方向）→ 未解釋
            missing_note: str = ""
            category: Optional[str] = blocked.get(symbol)
            if category is None:
                if ran_cleanly and all(_is_opening_order(o) for o in bt_orders):
                    category = CATEGORY_SNAPSHOT_GAP
                    missing_note = (
                        "回測以收盤價觸發開倉、實盤決策時的快照未觸發；當天段落正常結束"
                    )
                else:
                    category = CATEGORY_UNEXPLAINED
            diffs.append(
                _make_diff(
                    len(diffs) + 1,
                    symbol,
                    side,
                    category,
                    "（實盤沒有送出這張單）",
                    "；".join(summarize(order) for order in bt_orders),
                    note=missing_note,
                )
            )
            continue

        volume_diff: Optional[ParityDiff] = _compare_volume(
            len(diffs) + 1, symbol, side, live_rows, bt_orders
        )
        if volume_diff is not None:
            diffs.append(volume_diff)
            continue

        fill_diff: Optional[ParityDiff] = _compare_fill(
            len(diffs) + 1, symbol, side, live_rows, bt_orders
        )
        if fill_diff is not None:
            diffs.append(fill_diff)

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


def segments_ran_cleanly(
    strategy_name: str,
    runs: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
) -> bool:
    """
    - Description:
        這支策略當天的交易段落是否都有跑、都正常結束，而且沒有 CRITICAL 事件

        回測多出一張開倉單時，只有在這個條件下才能歸給快照口徑：段落中止、
        整天沒跑或出過 CRITICAL 的日子，實盤沒送單可能是程式出事，
        歸成快照口徑就把它蓋掉了（2026-10-07 期貨尾盤段中止就是這種日子）。

        **認不出策略的執行紀錄一律不算**：`live_run.strategy_params_json` 在
        2026-10-08 以前沒有寫入，那些日子判不出段落屬於誰，寧可留在未解釋。
    - Parameters:
        - strategy_name: str
            策略名
        - runs: Sequence[Dict[str, Any]]
            當日 `live_run` 的列
        - events: Sequence[Dict[str, Any]]
            這支策略當日的 `live_risk_event` 列
    - Return:
        - bool
            段落都正常、且沒有 CRITICAL 事件
    """

    own: List[Dict[str, Any]] = [
        run
        for run in runs
        if str(run.get("phase")) in TRADING_PHASES
        and strategy_name in _run_strategies(run)
    ]
    if not own:
        return False
    if any(str(run.get("end_reason")) != LiveTradeDAO.END_REASON_NORMAL for run in own):
        return False
    return not any(
        str(event.get("severity")) == NotifyLevel.CRITICAL.value for event in events
    )


def _run_strategies(run: Dict[str, Any]) -> Set[str]:
    """一筆執行紀錄載入的策略；欄位為空或格式不對時回空集合"""

    raw: Any = run.get("strategy_params_json")
    if not raw:
        return set()
    try:
        return {str(name) for name in json.loads(str(raw)).get("strategies", [])}
    except (ValueError, AttributeError):
        return set()


def _is_opening_order(order: BaseOrder) -> bool:
    """回測的這張委託是不是開倉單"""

    return (
        order.position_type is PositionType.LONG and order.action is Action.BUY
    ) or (order.position_type is PositionType.SHORT and order.action is Action.SELL)


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


# 送出後沒有（完全）成交的狀態。被拒、送單失敗不在內——它們另歸 `SEND_FAILED`
# （見上方 `_SEND_FAILED_STATUSES`），數量不同時由 `_compare_volume()` 歸因。
# **`PENDING_SUBMIT` 也算**：盤後比對時它應該早已被日終標記成已撤，還留著代表
# 狀態沒跟上券商，但這張單一定沒有成交。不算進來的話，兩邊數量相同就判成
# 「沒有差異」——2026-10-07 三張被撤掉的單就會這樣在報表上消失
_SEND_FAILED_STATUSES: Set[str] = {
    LiveOrderStatus.FAILED.value,
    LiveOrderStatus.REJECTED.value,
}

_UNFILLED_STATUSES: Set[str] = {
    LiveOrderStatus.PENDING_SUBMIT.value,
    LiveOrderStatus.SUBMITTED.value,
    LiveOrderStatus.PARTIALLY_FILLED.value,
    LiveOrderStatus.CANCELLED.value,
}


def _compare_fill(
    seq: int,
    symbol: str,
    side: str,
    live_rows: Sequence[Dict[str, Any]],
    bt_orders: Sequence[BaseOrder],
) -> Optional[ParityDiff]:
    """
    兩邊數量相同時，比實盤有沒有送出、有沒有成交；回測那邊一律視為成交

    **送單失敗優先**：委託沒到券商（`FAILED`）或被券商拒單（`REJECTED`），
    就談不上成交與否，歸 `SEND_FAILED`。

    多張同鍵委託只要有一張沒成交就算一筆差異，類別取第一張沒成交的委託：
    同一支策略同一個標的同一方向，一天之內的執行方式不會不同。
    """

    failed: List[Dict[str, Any]] = [
        row for row in live_rows if str(row.get("status")) in _SEND_FAILED_STATUSES
    ]
    if failed:
        return _make_diff(
            seq,
            symbol,
            side,
            CATEGORY_SEND_FAILED,
            "；".join(summarize_row(row) for row in live_rows),
            "；".join(summarize(order) for order in bt_orders),
            note="；".join(
                str(row.get("reject_reason") or row.get("status")) for row in failed
            ),
        )

    unfilled: List[Dict[str, Any]] = [
        row
        for row in live_rows
        if str(row.get("status")) in _UNFILLED_STATUSES
        and int(row.get("filled_volume") or 0) < int(row.get("volume") or 0)
    ]
    if not unfilled:
        return None

    filled: int = sum(int(row.get("filled_volume") or 0) for row in live_rows)
    volume: int = sum(int(row.get("volume") or 0) for row in live_rows)
    return _make_diff(
        seq,
        symbol,
        side,
        _unfilled_category(unfilled[0]),
        "；".join(summarize_row(row) for row in live_rows),
        "；".join(summarize(order) for order in bt_orders),
        note=f"實盤成交 {filled}／{volume}，回測以策略給的價全數成交",
    )


def _unfilled_category(row: Dict[str, Any]) -> str:
    """依執行方式與委託效期判斷沒成交的原因（見 `CATEGORY_LIMIT_UNFILLED`）"""

    style: str = str(row.get("execution_style") or "")
    if style == ExecutionStyle.LIMIT.value:
        return CATEGORY_LIMIT_UNFILLED
    if (
        style == ExecutionStyle.MARKET.value
        and str(row.get("order_type") or "") == OrderType.ROD.value
    ):
        return CATEGORY_LOCKED_AT_LIMIT
    return CATEGORY_UNFILLED


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
