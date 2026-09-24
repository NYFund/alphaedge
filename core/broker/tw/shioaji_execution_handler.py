import datetime
import json
import queue
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from loguru import logger

from core.config.settings import get_live_timezone
from core.models import ExecutionReport, OrderStatusEvent
from core.utils import Action, OrderState

"""
回報正規化：Shioaji 的 callback → 本專案的事件 → queue

**回報是實盤唯一可信的成交來源**，而且它有三個不能假設的性質：
1. **不保證順序**：成交回報可能比委託確認先到，所以 OMS 要以「成交即代表已提交」處理。
2. **不保證不重複**：斷線重連後同一筆可能再推一次，故每種事件都有去重鍵。
3. **不保證完整**：漏接就是漏接，只能靠盤後對帳發現。

**回呼只做兩件事：轉成事件、丟進 queue。** 回呼跑在 Shioaji 的內部執行緒上，
在裡面查 DB、下單或做重運算會拖住整條回報接收路徑——而那時正在成交。

回呼裡**任何例外都必須被吞掉**：往上拋只會讓 Shioaji 的執行緒死掉，
之後所有回報靜默消失，而程式看起來還活著。
"""

# 券商成功回應的 `op_code`
OP_CODE_SUCCESS: str = "00"


class ShioajiExecutionHandler:
    """
    - Description:
        把 Shioaji 的委託／成交回呼轉成 `OrderStatusEvent` 與 `ExecutionReport`

        四種 `OrderState` 各有 parser：`StockDeal`／`FuturesDeal` 是成交，
        `StockOrder`／`FuturesOrder` 是委託狀態事件。
    """

    def __init__(
        self,
        execution_queue: queue.Queue,
        record_path: Optional[Path] = None,
        on_first_report_ts: Optional[Callable[[datetime.datetime], None]] = None,
        futures_symbol: Optional[Callable[[str], str]] = None,
    ) -> None:
        """
        - Description:
            建立回報處理器
        - Parameters:
            - execution_queue: queue.Queue
                事件佇列（與 broker 共用同一個）
            - record_path: Optional[Path]
                錄製模式的 JSONL 檔路徑。錄下來的一天回報可以重放，
                那是唯一能把「當天到底收到什麼」重現出來的方式
            - on_first_report_ts: Optional[Callable[[datetime.datetime], None]]
                收到第一筆帶時戳的回報時呼叫，用來做精確的時鐘偏差檢查
                （登入時只拿得到合約檔的日期，沒有秒）
            - futures_symbol: Optional[Callable[[str], str]]
                期貨月份字母碼（`TXFJ6`）→ 專案代號（`TX202610`）；None 時不轉換
        """

        self.execution_queue: queue.Queue = execution_queue
        self.record_path: Optional[Path] = record_path
        self._on_first_report_ts: Optional[Callable[[datetime.datetime], None]] = (
            on_first_report_ts
        )
        self._clock_checked: bool = False
        self._futures_symbol: Optional[Callable[[str], str]] = futures_symbol

    # === 註冊 ===
    def register(self, api: Any) -> None:
        """把回呼掛上 Shioaji API"""

        api.set_order_callback(self.on_order_event)
        logger.info("Shioaji 委託／成交回呼已註冊")

    # === 回呼 ===
    def on_order_event(self, stat: Any, msg: Dict[str, Any]) -> None:
        """
        - Description:
            Shioaji 的回呼進入點

            **整個函式被 try 包住**：回呼跑在 Shioaji 的執行緒上，例外往上拋會讓
            那條執行緒死掉，之後所有回報靜默消失——程式看起來還活著，部位也還在
            場上。寧可丟掉一筆有問題的回報並留下 log，也不要丟掉之後的每一筆。
        - Parameters:
            - stat: Any
                `shioaji.OrderState`
            - msg: Dict[str, Any]
                回報內容
        """

        try:
            self._record(stat, msg)
            event: Optional[Union[ExecutionReport, OrderStatusEvent]] = self.parse(
                stat, msg
            )
            if event is None:
                return
            self._check_clock_once(event)
            self.execution_queue.put(event)
        except Exception as exc:
            logger.opt(exception=True).error(f"回報處理失敗（已略過本筆）：{exc}")

    def parse(
        self, stat: Any, msg: Dict[str, Any]
    ) -> Optional[Union[ExecutionReport, OrderStatusEvent]]:
        """
        - Description:
            依 `OrderState` 分派到對應的 parser
        - Parameters:
            - stat: Any
                回報種類
            - msg: Dict[str, Any]
                回報內容
        - Return:
            - Optional[Union[ExecutionReport, OrderStatusEvent]]
                轉換後的事件；不認得的種類回 None
        """

        # `stat` 是 `str` Enum，比的是字串值；本專案那份由
        # `tests/test_order_state_parity.py` 盯住不會與 Shioaji 漂開
        event: Optional[Union[ExecutionReport, OrderStatusEvent]] = None
        if stat in (OrderState.StockDeal, OrderState.FuturesDeal):
            event = self.parse_deal(msg)
        elif stat in (OrderState.StockOrder, OrderState.FuturesOrder):
            event = self.parse_order_event(msg)

        if event is not None:
            # 期貨回報的代碼是月份字母碼，要換成與 `FuturesOrder.symbol` 相同的
            # `{商品}{YYYYMM}`：歸屬帳、帳戶同步與對帳都以它對應部位
            if stat in (OrderState.FuturesDeal, OrderState.FuturesOrder) and (
                self._futures_symbol is not None
            ):
                event.symbol = self._futures_symbol(event.symbol)
            return event

        logger.warning(f"未知的回報種類：{stat!r}")
        return None

    # === Parser ===
    def parse_deal(self, msg: Dict[str, Any]) -> ExecutionReport:
        """
        - Description:
            成交回報 → `ExecutionReport`

            股票與期貨的成交回報欄位大致相同（`trade_id`／`seqno`／`code`／
            `action`／`price`／`quantity`／`ts`），差別在期貨多了契約相關欄位，
            那些留在 `raw` 裡，需要時再取。
        - Parameters:
            - msg: Dict[str, Any]
                成交回報內容
        - Return:
            - ExecutionReport
                正規化後的成交
        """

        return ExecutionReport(
            broker_seqno=self._as_text(msg.get("seqno")),
            broker_trade_id=self._as_text(msg.get("trade_id")),
            symbol=self._as_text(msg.get("code")),
            action=self._parse_action(msg.get("action")),
            price=float(msg.get("price") or 0.0),
            volume=int(msg.get("quantity") or 0),
            ts=self._parse_timestamp(msg.get("ts")),
            raw=dict(msg),
        )

    def parse_order_event(self, msg: Dict[str, Any]) -> OrderStatusEvent:
        """
        - Description:
            委託狀態回報 → `OrderStatusEvent`

            這類回報是巢狀的：`operation`（做了什麼、成功與否）、`order`（委託內容）、
            `status`（交易所回應）、`contract`（商品）。**逐層 `.get()` 取值**，
            因為不同操作別帶的欄位不一樣，硬取會在某個分支拋 KeyError，
            而那會發生在券商的執行緒裡。
        - Parameters:
            - msg: Dict[str, Any]
                委託回報內容
        - Return:
            - OrderStatusEvent
                正規化後的狀態事件
        """

        operation: Dict[str, Any] = msg.get("operation") or {}
        order: Dict[str, Any] = msg.get("order") or {}
        status: Dict[str, Any] = msg.get("status") or {}
        contract: Dict[str, Any] = msg.get("contract") or {}

        return OrderStatusEvent(
            broker_seqno=self._as_text(order.get("seqno") or status.get("id")),
            broker_order_id=self._as_text(order.get("ordno")) or None,
            op_type=self._as_text(operation.get("op_type")),
            op_code=self._as_text(operation.get("op_code")),
            op_msg=self._as_text(operation.get("op_msg")),
            symbol=self._as_text(contract.get("code")),
            custom_field=self._as_text(order.get("custom_field")),
            exchange_ts=self._parse_timestamp(status.get("exchange_ts")),
            raw=dict(msg),
        )

    # === 工具 ===
    @staticmethod
    def _as_text(value: Any) -> str:
        """
        統一轉成字串

        券商的序號在不同欄位有時是 `int`、有時是 `str`。不統一的話，
        去重鍵會出現 `123` 與 `"123"` 兩個版本，同一筆回報被當成兩筆。
        """

        return "" if value is None else str(value)

    @staticmethod
    def _parse_action(value: Any) -> Action:
        """
        券商的買賣別 → 本專案的 `Action`

        **依值轉換**：兩邊的成員名不同（`Buy` vs `BUY`）。認不得時預設為 BUY
        會把賣單記成買單，故一律拋出交由外層記錄並略過本筆。
        """

        text: str = "" if value is None else str(value)
        for member in Action:
            if member.value == text:
                return member
        raise ValueError(f"未知的買賣別：{text!r}")

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime.datetime]:
        """
        時戳 → 台北時區的 aware datetime

        券商給的是 epoch 秒數（浮點）。**不可當成 naive 本地時間**：
        主機時區是 UTC 時，整條時間軸會平移八小時，而段落判定全建立在它上面。
        """

        if value is None or value == "":
            return None
        if isinstance(value, datetime.datetime):
            return value
        try:
            return datetime.datetime.fromtimestamp(float(value), tz=get_live_timezone())
        except (TypeError, ValueError, OSError):
            logger.warning(f"無法解析回報時戳：{value!r}")
            return None

    def _check_clock_once(
        self, event: Union[ExecutionReport, OrderStatusEvent]
    ) -> None:
        """以第一筆帶時戳的回報做一次時鐘偏差檢查"""

        if self._clock_checked or self._on_first_report_ts is None:
            return

        ts: Optional[datetime.datetime] = getattr(event, "ts", None) or getattr(
            event, "exchange_ts", None
        )
        if ts is None:
            return

        self._clock_checked = True
        self._on_first_report_ts(ts)

    def _record(self, stat: Any, msg: Dict[str, Any]) -> None:
        """
        把原始回報附加到 JSONL

        **錄製失敗不可影響交易**：整段包 try。錄不到只是少了重放素材，
        錄製把回呼弄死才是真的事故。
        """

        if self.record_path is None:
            return

        try:
            line: str = json.dumps(
                {"stat": str(getattr(stat, "value", stat)), "msg": msg},
                ensure_ascii=False,
                default=str,
            )
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            with self.record_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        except Exception as exc:
            logger.opt(exception=True).warning(f"回報錄製失敗（忽略）：{exc}")
