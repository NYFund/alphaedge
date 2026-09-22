import argparse
import datetime
import sqlite3
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

from core.config import TW_TRADING_DB_PATH

"""
模擬環境是否保留隔夜部位？——唯讀分析 `live_position_snapshot`

**動機**：模擬環境的多日演練有四條驗收項目建立在「隔夜部位」上（券商端部位
重建、對帳零差異、次日補平、跨日模式延續）。若 Shioaji 模擬環境每晚把部位清掉，
那麼從第 2 天起**本地歸屬帳與券商部位必然不一致**，而 `Reconciler` 不一致時會
送降級事件、驗收標準 3 又要求「對帳不一致時停止開新倉」——後四天會在降級狀態
下空轉，且「收盤後對帳零差異」在設計上就達不到。

**本工具不連券商、不寫任何東西**：`Reconciler._write_snapshots()` 的註解寫明
「一致時也要寫」，所以每次對帳都會落下 account／local／broker 三份快照。
資料收集本來就是自動的，這裡只負責讀出來回答那個問題。

**怎麼判讀**：看相鄰兩個日期之間 `source='broker'` 的部位。
- 前一日收盤有部位、次日開盤**整批消失**且本地沒有對應的平倉紀錄 → 模擬環境清倉。
- 部位原樣留著 → 模擬環境保留隔夜部位，五日演練的隔夜項目有效。

**一天的資料不足以下結論**：至少要兩個跑過對帳的日期才比得出來。
注意判準是「當天有沒有跑過對帳」而不是「有沒有 broker 列」——券商端沒有部位時
一列都不會寫，用後者篩選會讓「被清倉」那天整個消失。
"""


# 對帳兩邊的來源名稱；`account` 是帳戶層合計，`local` 是逐策略歸屬帳
BROKER_SOURCE: str = "broker"
ACCOUNT_SOURCE: str = "account"
LOCAL_SOURCE: str = "local"

# (symbol, direction) → volume
PositionMap = Dict[Tuple[str, str], int]


def load_snapshots(conn: sqlite3.Connection) -> Dict[str, Dict[str, PositionMap]]:
    """
    - Description:
        讀出每個日期、每個來源的部位
    - Parameters:
        - conn: sqlite3.Connection
            `tw_trading.db` 的唯讀連線
    - Return:
        - Dict[str, Dict[str, PositionMap]]
            `{日期: {來源: {(標的, 方向): 數量}}}`
    """

    snapshots: Dict[str, Dict[str, PositionMap]] = defaultdict(
        lambda: defaultdict(dict)
    )

    query: str = """
        SELECT date, source, symbol, direction, volume
        FROM live_position_snapshot
        ORDER BY date, source, symbol
    """
    for date, source, symbol, direction, volume in conn.execute(query):
        snapshots[str(date)][str(source)][(str(symbol), str(direction))] = int(volume)

    return snapshots


def load_closing_records(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    """
    - Description:
        讀出每個日期本地記錄的平倉標的

        用來區分「部位消失是因為我們平掉了」還是「模擬環境自己清掉了」——
        少了這一層，正常的平倉會被誤判成環境清倉。
    - Parameters:
        - conn: sqlite3.Connection
            `tw_trading.db` 的唯讀連線
    - Return:
        - Dict[str, Set[str]]
            `{日期: {當日平倉的標的}}`
    """

    closed: Dict[str, Set[str]] = defaultdict(set)

    query: str = """
        SELECT substr(closed_at, 1, 10) AS close_date, symbol
        FROM live_position_lot
        WHERE closed_at IS NOT NULL
    """
    for close_date, symbol in conn.execute(query):
        closed[str(close_date)].add(str(symbol))

    return closed


def describe(positions: PositionMap) -> str:
    """把部位對照表寫成一行；空的時候明講是空的"""

    if not positions:
        return "（無部位）"

    return "、".join(
        f"{symbol} {direction} {volume}"
        for (symbol, direction), volume in sorted(positions.items())
    )


def report_by_date(snapshots: Dict[str, Dict[str, PositionMap]]) -> None:
    """逐日列出三個來源的部位"""

    print("=" * 72)
    print("各日期的部位快照")
    print("=" * 72)

    for date in sorted(snapshots):
        print(f"\n{date}")
        for source in (BROKER_SOURCE, ACCOUNT_SOURCE, LOCAL_SOURCE):
            positions: PositionMap = snapshots[date].get(source, {})
            print(f"  {source:8s} {describe(positions)}")


def report_overnight(
    snapshots: Dict[str, Dict[str, PositionMap]],
    closed: Dict[str, Set[str]],
) -> Optional[bool]:
    """
    - Description:
        比對相鄰日期的券商部位，回答「模擬環境有沒有保留隔夜部位」
    - Parameters:
        - snapshots: Dict[str, Dict[str, PositionMap]]
            各日期各來源的部位
        - closed: Dict[str, Set[str]]
            各日期本地記錄的平倉標的
    - Return:
        - Optional[bool]
            True 表示有保留、False 表示會清倉；資料不足時為 None
    """

    # **以「當天有沒有跑過對帳」為準，不是「有沒有 broker 列」**：
    # `_write_snapshots()` 是迭代券商部位清單寫的，券商端沒有部位時一列都不會寫。
    # 若用 broker 列篩日期，「被清倉」那天會整個消失，於是最該被偵測到的情況
    # 反而變成「資料不足」——正好漏掉要找的東西。
    dates: List[str] = sorted(date for date in snapshots if snapshots[date])

    print()
    print("=" * 72)
    print("隔夜比對（只看 source='broker'）")
    print("=" * 72)

    if len(dates) < 2:
        print(
            f"\n資料不足：只有 {len(dates)} 個日期跑過對帳，至少要 2 個才比得出隔夜行為。"
        )
        if dates:
            print(
                f"目前僅有 {dates[0]}："
                f"{describe(snapshots[dates[0]].get(BROKER_SOURCE, {}))}"
            )
        return None

    survived_any: bool = False
    wiped_any: bool = False

    for before, after in zip(dates, dates[1:]):
        prev: PositionMap = snapshots[before].get(BROKER_SOURCE, {})
        curr: PositionMap = snapshots[after].get(BROKER_SOURCE, {})

        # 本地有記平倉的標的不算「被環境清掉」
        closed_symbols: Set[str] = closed.get(after, set()) | closed.get(before, set())

        vanished: List[Tuple[str, str]] = [
            key for key in prev if key not in curr and key[0] not in closed_symbols
        ]
        survived: List[Tuple[str, str]] = [key for key in prev if key in curr]

        print(f"\n{before} → {after}")
        print(f"  前一日券商部位：{describe(prev)}")
        print(f"  次日券商部位：  {describe(curr)}")
        if closed_symbols:
            print(f"  本地記錄的平倉：{'、'.join(sorted(closed_symbols))}")

        if not prev:
            print("  → 前一日本來就沒有部位，這一組比不出東西")
            continue

        if vanished and not survived:
            wiped_any = True
            print(
                f"  → **整批消失**（{len(vanished)} 筆），且本地沒有對應的平倉紀錄"
                "，指向模擬環境清倉"
            )
        elif vanished:
            wiped_any = True
            print(
                f"  → 部分消失（{len(vanished)} 筆消失／{len(survived)} 筆留存），"
                "需人工判讀"
            )
        else:
            survived_any = True
            print(f"  → 全數留存（{len(survived)} 筆），模擬環境保留隔夜部位")

    print()
    if wiped_any and not survived_any:
        print("結論：模擬環境**不保留**隔夜部位。")
        return False
    if survived_any and not wiped_any:
        print("結論：模擬環境**保留**隔夜部位，五日演練的隔夜驗收項目有效。")
        return True

    print("結論：兩種情況都出現過，要人工判讀（可能中途有手動平倉或環境重置）。")
    return None


def report_mismatch(snapshots: Dict[str, Dict[str, PositionMap]]) -> None:
    """列出每日 local 合計與 broker 的差異，也就是對帳缺口"""

    print()
    print("=" * 72)
    print("每日對帳缺口（Σ local ＝ broker 才算一致）")
    print("=" * 72)

    for date in sorted(snapshots):
        broker: PositionMap = snapshots[date].get(BROKER_SOURCE, {})
        account: PositionMap = snapshots[date].get(ACCOUNT_SOURCE, {})

        keys: Set[Tuple[str, str]] = set(broker) | set(account)
        diffs: List[str] = [
            f"{symbol} {direction}：本地 {account.get((symbol, direction), 0)}"
            f" vs 券商 {broker.get((symbol, direction), 0)}"
            for symbol, direction in sorted(keys)
            if account.get((symbol, direction), 0) != broker.get((symbol, direction), 0)
        ]

        if diffs:
            print(f"\n{date}  ✗ {len(diffs)} 筆不一致")
            for line in diffs:
                print(f"    {line}")
        else:
            print(f"\n{date}  ✓ 一致")


def report_risk_events(conn: sqlite3.Connection) -> None:
    """
    列出對帳不一致的風控事件

    這是「降級是否已經發生」的直接證據：`Reconciler` 不一致時會送降級事件，
    而驗收標準要求「對帳不一致時停止開新倉」——這張表有列，就代表那天在降級。
    """

    print()
    print("=" * 72)
    print("對帳不一致事件（category = RECONCILE_MISMATCH）")
    print("=" * 72)

    query: str = """
        SELECT occurred_at, severity, strategy_name, symbol, message
        FROM live_risk_event
        WHERE category = 'RECONCILE_MISMATCH'
        ORDER BY occurred_at
    """
    rows: List[Tuple[str, str, Optional[str], Optional[str], str]] = list(
        conn.execute(query)
    )

    if not rows:
        print("\n（無）")
        return

    for occurred_at, severity, strategy_name, symbol, message in rows:
        scope: str = " / ".join(part for part in (strategy_name, symbol) if part)
        print(f"\n  {occurred_at} [{severity}] {scope}")
        print(f"    {message}")


def main() -> int:
    """讀 `tw_trading.db` 並回答隔夜部位的問題；不連券商、不寫入"""

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="檢查模擬環境是否保留隔夜部位（唯讀）"
    )
    parser.add_argument(
        "--db",
        default=str(TW_TRADING_DB_PATH),
        help="紀錄庫路徑（預設為正式的 tw_trading.db）",
    )
    args: argparse.Namespace = parser.parse_args()

    # 唯讀開啟：這支工具在演練進行中會被執行，絕不能干擾正在寫入的行程
    conn: sqlite3.Connection = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    print(f"紀錄庫：{args.db}")
    print(f"查詢時間：{datetime.datetime.now().isoformat(timespec='seconds')}")

    snapshots: Dict[str, Dict[str, PositionMap]] = load_snapshots(conn)
    if not snapshots:
        print("\n紀錄庫內尚無任何部位快照——演練還沒開始，或對帳還沒跑過。")
        conn.close()
        return 0

    closed: Dict[str, Set[str]] = load_closing_records(conn)

    report_by_date(snapshots)
    report_overnight(snapshots, closed)
    report_mismatch(snapshots)
    report_risk_events(conn)

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
