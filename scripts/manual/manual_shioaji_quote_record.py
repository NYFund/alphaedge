import argparse
import datetime
import json
import queue
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from loguru import logger

from core.broker.rate_limiter import RateLimiter
from core.broker.tw.quote_replay import RECORDED_FIELD_TYPES
from core.broker.tw.shioaji_quote_stream import ShioajiQuoteStream
from core.broker.tw.shioaji_session import ShioajiSession
from core.config.settings import now_live

"""
盤中行情錄製：把 Shioaji 的逐筆與委買賣回呼原樣存成 JSONL，並印出欄位清單

**這支腳本要回答的問題是「券商真的推什麼欄位」**，不是「程式跑不跑得動」。
`ShioajiQuoteStream` 的入列行為已由單元測試覆蓋；覆蓋不到的是欄位名與型別，
而那正是本專案一再踩到的地方——`Snapshot.ts` 是奈秒、`volume` 要取 `total_volume`、
合約檔的 `update_date` 是 `YYYY/MM/DD` 不是 ISO，**三次都是實連才發現的**，
而且三次的共同症狀都是「單元測試一直是綠的」。

tick 訊息 → `StockQuote` 的轉換目前還沒寫。照文件猜再配上 `.get()` 預設值，
欄位名錯了只會讓報價靜靜變成 0，策略再也不產生訊號而且沒有任何錯誤——
所以先錄一份真資料，把欄位釘死之後再寫轉換。

執行（一律在專案根目錄以 `-m`，且要在**交易日盤中**）：

    .venv/bin/python -m scripts.manual.manual_shioaji_quote_record
    .venv/bin/python -m scripts.manual.manual_shioaji_quote_record --symbols 2330,2317 --seconds 120

**本腳本只訂閱與接收，不下單、不連正式環境。**
"""

DEFAULT_SYMBOLS: Tuple[str, ...] = ("2330", "2317", "2454")
DEFAULT_SECONDS: int = 60

# 原始訊息落地的位置。**放 `data/records/` 而不是 `results/`**：
# 它是素材不是產出，重放與欄位核對都要拿它當依據
DEFAULT_OUTPUT_DIR: Path = Path("data/records")


def parse_arguments() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="錄製 Shioaji 盤中行情回呼（模擬環境）"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_SYMBOLS),
        help="逗號分隔的股票代號",
    )
    parser.add_argument("--seconds", type=int, default=DEFAULT_SECONDS, help="錄製秒數")
    parser.add_argument(
        "--output", type=str, default=None, help="輸出 JSONL 路徑；預設依日期命名"
    )
    parser.add_argument(
        "--no-bidask", action="store_true", help="只訂閱逐筆，不訂委買賣"
    )
    return parser.parse_args()


# 欄位名取自 `RECORDED_FIELD_TYPES`，與重放端共用同一份，錄得下來的就還原得回去。
#
# 執行期真正拿到的是原生擴充物件：shioaji 1.3.3 的 C 擴充物件**沒有 `__dict__`、
# `dict()`、`model_dump()`，連 `dir()` 都是空的**，只能照名字 `getattr`。
# 第一次錄製就是栽在這裡：447 筆全錄成 `{}`
_STUB_FIELDS: Dict[str, Tuple[str, ...]] = {
    name: tuple(fields) for name, fields in RECORDED_FIELD_TYPES.items()
}


def to_plain(message: Any) -> Any:
    """
    把回呼訊息轉成可序列化的結構

    **依序試四種取法而不是只試一種**：pydantic 的 `model_dump()`／`dict()`、
    一般物件的 `__dict__`，最後才是照 `RECORDED_FIELD_TYPES` 的欄位名逐一 `getattr`。
    硬寫一種，升版換了實作就整份錄製變成空的——而錄製本來就是為了升版時能比對。
    """

    for attribute in ("model_dump", "dict"):
        method: Any = getattr(message, attribute, None)
        if callable(method):
            try:
                extracted: Any = method()
                if extracted:
                    return extracted
            except Exception:
                continue

    if getattr(message, "__dict__", None):
        return dict(vars(message))

    fields: Tuple[str, ...] = _STUB_FIELDS.get(type(message).__name__, ())
    if fields:
        return {name: getattr(message, name, None) for name in fields}

    # 連欄位名都對不上時**保留 repr**：空 dict 會讓人以為「這類訊息沒有內容」，
    # 而真正的問題是取法不對——那正是第一次錄製浪費掉的原因
    return {"__unparsed_repr__": repr(message), "__type__": type(message).__name__}


def describe_fields(samples: List[Dict[str, Any]]) -> List[str]:
    """
    列出這一類訊息出現過的欄位、型別與一個範例值

    **要列型別**：`ts` 是奈秒整數還是 datetime、價格是 float 還是 Decimal，
    決定了轉換要怎麼寫；只看欄位名會再猜錯一次。
    """

    seen: Dict[str, Tuple[str, Any]] = {}
    for sample in samples:
        for key, value in sample.items():
            if key not in seen:
                seen[key] = (type(value).__name__, value)

    return [
        f"  {key}: {value!r}（{type_name}）"
        for key, (type_name, value) in sorted(seen.items())
    ]


def main() -> int:
    """
    - Description:
        登入 → 訂閱 → 錄製 N 秒 → 印出欄位清單 → 登出
    - Return:
        - int
            0 成功；1 連線失敗；2 完全沒收到行情
    """

    args: argparse.Namespace = parse_arguments()
    symbols: List[str] = [s.strip() for s in args.symbols.split(",") if s.strip()]

    started: datetime.datetime = now_live()
    output: Path = (
        Path(args.output)
        if args.output
        else DEFAULT_OUTPUT_DIR / f"quotes_{started:%Y%m%d_%H%M}.jsonl"
    )

    session: ShioajiSession = ShioajiSession(simulation=True)
    events: queue.Queue = queue.Queue()

    try:
        session.connect()
    except Exception as exc:
        # 不印 traceback：連不上的原因幾乎都是金鑰、權限或非交易時段，
        # 堆疊對排查沒有幫助，只會把真正的訊息推到畫面外
        logger.error(f"登入失敗：{exc}")
        return 1

    try:
        stream: ShioajiQuoteStream = ShioajiQuoteStream(
            session.api, RateLimiter(), events, now_provider=now_live
        )
        stream.register_callbacks()

        contracts: List[Any] = []
        for symbol in symbols:
            contract: Any = session.api.Contracts.Stocks.get(symbol)
            if contract is None:
                logger.warning(f"查不到合約 {symbol}，略過")
                continue
            contracts.append(contract)

        if not contracts:
            logger.error("沒有任何可訂閱的合約")
            return 2

        stream.subscribe(contracts, with_bidask=not args.no_bidask)
        logger.info(
            f"開始錄製 {args.seconds} 秒（台北 {now_live():%H:%M:%S}）→ {output}"
        )

        deadline: datetime.datetime = started + datetime.timedelta(seconds=args.seconds)
        by_kind: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        codes: Set[str] = set()
        total: int = 0

        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as handle:
            while now_live() < deadline:
                try:
                    kind, exchange, message = events.get(timeout=1.0)
                except queue.Empty:
                    continue

                payload: Any = to_plain(message)
                total += 1
                if isinstance(payload, dict):
                    by_kind[kind].append(payload)
                    code: Any = payload.get("code")
                    if code:
                        codes.add(str(code))

                handle.write(
                    json.dumps(
                        {
                            "kind": kind,
                            "exchange": str(getattr(exchange, "value", exchange)),
                            "received_at": now_live().isoformat(),
                            "message": payload,
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                    + "\n"
                )

        stream.unsubscribe(contracts, with_bidask=not args.no_bidask)

        logger.info(f"=== 共收到 {total} 筆，涵蓋 {sorted(codes)} ===")
        if total == 0:
            logger.error(
                "完全沒有收到行情。請確認：現在是交易日盤中嗎？"
                "訂閱的標的今天有成交嗎？模擬環境是否推送行情？"
            )
            return 2

        for kind, samples in sorted(by_kind.items()):
            logger.info(f"=== {kind}：{len(samples)} 筆 ===")
            for line in describe_fields(samples):
                logger.info(line)

        logger.info(f"原始訊息已寫入 {output}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
