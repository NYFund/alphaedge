import datetime
from typing import Any, List, Optional, Tuple

from core.dao.tw.live_trade_dao import LiveTradeDAO
from core.live.risk.trading_mode import TradingMode, TradingModeState

"""
並行段落：排程刻意重疊，而崩潰判定與模式落地都假設了「不重疊」

台股尾盤段 13:20 啟動、13:35 收線，期貨尾盤段 13:28 啟動——**每天重疊 7 分鐘**；
開盤段同理（08:30→09:05 對上 08:40）。兩個後果都不會有錯誤訊息：

1. 後啟動者把前者標成 `CRASHED` 並補上 `ended_at`，同時推一則 CRITICAL。
   每天都誤報的告警等於沒有告警。
2. **帳戶層 halt 被靜默解除**：被誤標那列的 `account_mode` 還是插入時的初始
   `NORMAL`（本段落沒有再降級，`update_account_mode()` 就不會被呼叫），
   後啟動者的 `get_last_account_mode()` 只讀已結束的紀錄，於是讀到 `NORMAL`。

第 2 點不只發生在重疊：**真正崩潰時也一樣**——繼承來的降級從來沒被寫下去過。
"""

NOW: datetime.datetime = datetime.datetime(2026, 9, 24, 13, 20)


def account_mode_of(dao: LiveTradeDAO, run_id: str) -> Optional[str]:
    """直接讀 `live_run` 那一格，不經過任何快取"""

    row: Optional[Tuple[Any, ...]] = dao.conn.execute(
        "SELECT account_mode FROM live_run WHERE run_id = ?", (run_id,)
    ).fetchone()
    return str(row[0]) if row else None


def start_run(dao: LiveTradeDAO, run_id: str, **extra: Any) -> None:
    """開一筆啟動紀錄（`ended_at` 為 NULL＝執行中）"""

    row: dict = {
        "run_id": run_id,
        "started_at": NOW,
        "phase": "close",
        "simulation": 1,
    }
    row.update(extra)
    dao.insert_run(row)


# === 繼承來的降級要當場落地 ===
def test_inherited_halt_is_recorded_on_this_run(dao: LiveTradeDAO) -> None:
    """
    `load()` 讀回 `REDUCE_ONLY` 之後要**寫進本次的紀錄**

    不寫的話，本次那一格會停在插入時的 `NORMAL`——而那一格正是下一個段落
    判斷「上次結束時是什麼模式」的依據。
    """

    start_run(dao, "run1")
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    start_run(dao, "run2")
    TradingModeState(dao, "run2", lambda: NOW).load()

    assert account_mode_of(dao, "run2") == "REDUCE_ONLY"


def test_inherited_halt_survives_a_crash(dao: LiveTradeDAO) -> None:
    """
    繼承降級的行程**崩潰**時，降級仍要讀得回來

    崩潰不會走到 `finish_run()`，所以那一格只剩 `load()` 有機會寫。
    沒寫的話，重啟的人會拿到一個乾淨的 `NORMAL`，帶著錯誤部位繼續交易——
    這正是「按重啟鍵就解除 halt」那個事故型態，只是換成隔了一個段落。
    """

    start_run(dao, "run1")
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    # run2 繼承降級後崩潰：沒有 finish_run
    start_run(dao, "run2")
    TradingModeState(dao, "run2", lambda: NOW).load()

    # run3 啟動，先標記崩潰的舊紀錄再讀模式（`prepare()` 的順序）
    start_run(dao, "run3")
    dao.mark_crashed_runs("run3", NOW)
    third: TradingModeState = TradingModeState(dao, "run3", lambda: NOW)
    third.load()

    assert third.account_mode is TradingMode.REDUCE_ONLY


def test_a_sibling_cannot_clear_the_halt_by_marking_this_run_crashed(
    dao: LiveTradeDAO,
) -> None:
    """
    **重疊段落不得清掉 halt**

    股票尾盤段還在跑（`ended_at IS NULL`），期貨尾盤段 13:28 啟動並把它標成
    `CRASHED`。若被標那列的模式還停在初始 `NORMAL`，期貨段就會讀到 `NORMAL`
    並照常開新倉——而帳戶其實是停機狀態，整個過程沒有任何警告。
    """

    start_run(dao, "yesterday")
    dao.finish_run("yesterday", NOW, "對帳不一致", "REDUCE_ONLY")

    # 股票尾盤段：繼承降級，仍在執行中
    start_run(dao, "stock_close")
    TradingModeState(dao, "stock_close", lambda: NOW).load()

    # 期貨尾盤段：標記「崩潰」的舊紀錄，然後讀模式
    start_run(dao, "futures_close")
    dao.mark_crashed_runs("futures_close", NOW)
    futures: TradingModeState = TradingModeState(dao, "futures_close", lambda: NOW)
    futures.load()

    assert futures.account_mode is TradingMode.REDUCE_ONLY, (
        "重疊的段落把帳戶層 halt 清掉了"
    )
    assert futures.allows_open("A") is False


# === 還活著的段落不是崩潰 ===
def test_a_still_running_sibling_is_not_marked_crashed(dao: LiveTradeDAO) -> None:
    """
    `ended_at IS NULL` 有**第三種**可能：另一個正常執行中的段落

    誤標的代價有三個：每天推一則 CRITICAL（天天誤報的告警等於沒有告警）、
    在對方還在送單時做一次「以券商為準重建」、以及在 `live_run` 上留下
    一筆假的崩潰紀錄——而那筆紀錄稍後會被對方自己的 `finish_run()` 覆寫，
    事後完全看不出發生過。
    """

    start_run(dao, "stock_close", pid=4242)
    start_run(dao, "futures_close", pid=4243)

    crashed: List[str] = dao.mark_crashed_runs(
        "futures_close", NOW, is_alive=lambda pid: pid == 4242
    )

    assert crashed == []
    assert account_mode_of(dao, "stock_close") is not None
    row: Optional[Tuple[Any, ...]] = dao.conn.execute(
        "SELECT ended_at FROM live_run WHERE run_id = 'stock_close'"
    ).fetchone()
    assert row[0] is None, "還在跑的段落被補上了 ended_at"


def test_a_dead_run_is_still_marked_crashed(dao: LiveTradeDAO) -> None:
    """
    真正死掉的行程仍要標記

    收緊判定不可以把原本擋得住的事故放過去——不標記的話，
    `get_last_account_mode()` 會跳過那一列（它只讀已結束的）。
    """

    start_run(dao, "dead", pid=9999)
    start_run(dao, "current", pid=4243)

    crashed: List[str] = dao.mark_crashed_runs(
        "current", NOW, is_alive=lambda pid: False
    )

    assert crashed == ["dead"]


def test_runs_without_a_pid_are_treated_as_dead(dao: LiveTradeDAO) -> None:
    """
    舊紀錄沒有 `pid`，一律當成崩潰

    欄位是後加的，既有的資料列都是 NULL。當成「還活著」的話，
    升級前留下的崩潰紀錄會永遠標不起來，降級狀態也就永遠讀不回。
    """

    start_run(dao, "legacy")
    start_run(dao, "current", pid=4243)

    assert dao.mark_crashed_runs("current", NOW, is_alive=lambda pid: True) == [
        "legacy"
    ]


# === 恢復與讀回綁在一起 ===
def test_resume_is_applied_by_load_itself(dao: LiveTradeDAO) -> None:
    """
    `--resume-trading` 要由 `load()` 一併套用

    拆成「呼叫端先 load 再 resume」的話，漏掉其中一半不會報錯——
    `AfterCloseRunner` 就只呼叫了 `load()`，於是
    `--phase after_close --resume-trading` 靜默無效：旗標被接受、
    halt 沒解除，而使用者會以為解除了。
    """

    start_run(dao, "run1")
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    start_run(dao, "run2")
    state: TradingModeState = TradingModeState(
        dao, "run2", lambda: NOW, resume_trading=True
    )
    state.load()

    assert state.account_mode is TradingMode.NORMAL
    assert account_mode_of(dao, "run2") == "NORMAL"


def test_resume_also_clears_strategy_level_degradation(dao: LiveTradeDAO) -> None:
    """
    策略層的降級同樣要解除

    2026-09-24 演練實測：`MomentumStrategy1` 因鉤子拋例外被降為 HALTED，
    只解除帳戶層的話，那支策略下次啟動照樣不交易。
    """

    start_run(dao, "run1")
    TradingModeState(dao, "run1", lambda: NOW).degrade(
        TradingMode.HALTED, "鉤子執行失敗", strategy_name="A"
    )
    dao.finish_run("run1", NOW, "正常結束", "NORMAL")

    start_run(dao, "run2")
    state: TradingModeState = TradingModeState(
        dao, "run2", lambda: NOW, resume_trading=True
    )
    state.load()

    assert state.effective_mode("A") is TradingMode.NORMAL
    assert state.allows_open("A") is True


def test_load_without_the_flag_keeps_the_halt(dao: LiveTradeDAO) -> None:
    """
    沒帶旗標就不可以恢復

    自動恢復等於賭「造成降級的那件事已經好了」，而會自動降級的條件
    多半是偵測不完整的異常。
    """

    start_run(dao, "run1")
    dao.finish_run("run1", NOW, "對帳不一致", "REDUCE_ONLY")

    start_run(dao, "run2")
    state: TradingModeState = TradingModeState(dao, "run2", lambda: NOW)
    state.load()

    assert state.account_mode is TradingMode.REDUCE_ONLY
