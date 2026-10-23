from typing import Any, Callable, Iterable, List, Optional, Sequence, Tuple

from loguru import logger

from core.pipeline.shared.graceful_stop import GracefulStop

"""
ETL 的兩個共用流程骨架：「疑似被擋就重試」與「逐子項隔離、最後彙總再拋」

兩者原本各有多份實作（重試：期貨行情、期貨籌碼；隔離：期貨行情、期貨保證金、
期貨籌碼、休市日），判準相同、註解互相指涉，改一份另一份不會跟著改。
這裡只提供流程，**判準與等待時間由呼叫端決定**：怎樣算被擋（有交易日卻拿不到、
沒拿到日盤）、等多久（固定、隨次數遞增、隨連續空產出遞增）都是各來源的事。
"""


def retry_if_blocked(
    fetch: Callable[[], Any],
    is_blocked: Callable[[Any], bool],
    attempts: int,
    delay_seconds: float,
    label: str,
    stop: Optional[GracefulStop] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> Tuple[Any, int]:
    """
    - Description:
        取一次；疑似被擋就等待後再取，最多取 `attempts` 次（含第一次）

        **等待隨次數線性遞增**（`delay_seconds × 第幾次重試`）：站方擋流量多半是暫時的，
        第一次短等就夠，越擋越久才給越長的冷卻。等待可被中止：`stop` 收到訊號就不再重試，
        把最後一次的結果原樣交回，由呼叫端決定怎麼記。
    - Parameters:
        - fetch: Callable[[], Any]
            取資料的動作（每次重試都重新呼叫）
        - is_blocked: Callable[[Any], bool]
            結果疑似被擋的判準；回 False 就直接回傳結果
        - attempts: int
            最多取幾次（含第一次）；1 表示不重試
        - delay_seconds: float
            第一次重試前的等待秒數；之後乘上重試次數
        - label: str
            log 用的來源名稱
        - stop: Optional[GracefulStop]
            中止旗標；等待期間收到訊號就停止重試
        - sleep: Optional[Callable[[float], None]]
            等待的實作；None 時有 `stop` 走 `stop.sleep()`，否則 `time.sleep()`
    - Return:
        - Tuple[Any, int]
            （最後一次的結果, 實際重試次數）
    """

    import time

    wait: Callable[[float], None] = sleep or (
        stop.sleep if stop is not None else time.sleep
    )

    result: Any = fetch()
    retries: int = 0
    while is_blocked(result) and retries < attempts - 1:
        retries += 1
        seconds: float = delay_seconds * retries
        logger.warning(f"[{label}] 疑似被擋，{seconds:g} 秒後重試（第 {retries} 次）")
        wait(seconds)
        if stop is not None and stop.requested:
            break
        result = fetch()

    return result, retries


def run_isolated(
    steps: Sequence[Tuple[str, Callable[[], Optional[Iterable[str]]]]],
    label: str,
) -> List[Tuple[str, str]]:
    """
    - Description:
        逐子項執行，一個子項失敗不擋後面的；回傳失敗清單讓呼叫端彙總後再拋

        **子項的失敗有兩種表達**：拋出任何例外（記成 `ExcType: 訊息`，含 traceback 進 log），
        或回傳一串失敗描述（例如被擋的月份）。兩種都只記下來、繼續下一個子項；
        全部跑完後有任何失敗，呼叫端必須讓行程非零結束，否則缺漏要靠事後對帳才會被發現。

        **對例外的盲捕是這裡的契約**：子項是一整條子管線（抓取→清洗→入庫），拋得出什麼
        由那一側決定；收斂成列舉的話，漏掉的那一種會讓後面的子項全部不跑，而行程仍以成功結束。
    - Parameters:
        - steps: Sequence[Tuple[str, Callable[[], Optional[Iterable[str]]]]]
            （子項名稱, 子項動作）；動作可回傳失敗描述的清單
        - label: str
            log 用的來源名稱
    - Return:
        - List[Tuple[str, str]]
            （子項名稱, 失敗描述），依執行順序
    """

    failures: List[Tuple[str, str]] = []

    for name, step in steps:
        try:
            reported: Optional[Iterable[str]] = step()
        except Exception as error:
            logger.opt(exception=True).error(
                f"[{label}] {name} 失敗，繼續下一個：{type(error).__name__}: {error}"
            )
            failures.append((name, f"{type(error).__name__}: {error}"))
            continue

        for item in reported or ():
            failures.append((name, item))

    return failures
