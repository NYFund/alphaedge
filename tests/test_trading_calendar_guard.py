import hashlib
import sqlite3
import statistics
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pytest

from core.config import (
    CHIP_TABLE_NAME,
    MARGIN_TABLE_NAME,
    PRICE_TABLE_NAME,
    STOCK_INFO_TABLE_NAME,
    TW_FUTURES_DB_PATH,
    TW_STOCK_DB_PATH,
)

"""日頻表的批次污染護欄

三支 cleaner 都是 `df.insert(0, "date", date)`——蓋上去的是**請求參數的日期**，
不是來源頁面內的日期。爬蟲端已由 `f278160` 補上休市判斷，但那道判斷靠的是站方
回覆的內容，站方行為一變就可能再破一次，而**壞掉的樣子不會讓任何既有測試變紅**：
缺資料會讓缺口統計出聲，多資料不會。

本檔用三個互補的判準把「壞掉但看起來正常」的批次釘住：

1. `test_no_non_trading_day_batches`：日期軸。某張表有、其他三張全無的日期。
2. `test_no_stale_replayed_batches`：內容軸。同一批內容被蓋上不同日期。
3. `test_no_single_market_batches`：市場軸。某一天只入庫了 TWSE 或只入庫了 TPEX。

以清理前的資料實測（2026-09-04／09-15 兩次清理）：
測試 1 抓到全部 21 個非交易日批次，測試 2 抓到 2 個重播群組共 7 個日期，
測試 3 抓到 11 天只有半個市場的批次（`chip` 9 天、`price` 2 天）。

**第 3 條是後來補上的，補的正是前兩條的盲區。** 第一次清理時有 5 天
（`2023-05-12`、`2024-02-16`、`2025-01-22`、`2025-03-24`、`2025-04-23`）
兩條都抓不到：它們是真實交易日，日期軸沒有異常，而壞掉的只是 TWSE 那半邊，
指紋取自正常的 TPEX 部分故不碰撞。當時的結論是「只能靠列數異常人工複查」，
但列數判準（低於鄰日中位數 85%）會連帶掃到本來就量少的日期而變成紅燈噪音。
**改看市場別分布就沒有這個問題**——量少的日子兩個市場會一起少，
而壞掉的日子是其中一個直接歸零，兩者差兩個數量級。
"""

# 四張表都還有資料的最後一天。**必須夾上界**——各表尾端落後不同
# （`chip` 補到 2026-06-18、`margin` 到 2026-08-14），不夾的話 `price` 領先的
# 那幾天會被誤判成「price 獨有」
COMMON_RANGE_END: str = "2026-06-18"

# 內容指紋取「絕對值最大的 N 筆」。取極端值是因為它們最不可能巧合相同：
# 買賣超為 0 或個位數張的個股每天都有一大票，拿來當指紋會誤報
FINGERPRINT_SIZE: int = 50

# 重播判準（`price`）：前 50 大裡有幾筆完全相同就算重播。被重播的常常只有半個
# 市場，另一半是當天真實的資料，故不能要求整份相同——修正前的 2020-04-14 與
# 2020-12-18 實測重疊 47 筆。取 40（八成）落在「半個市場被重播」與正常日之間
REPLAY_OVERLAP_THRESHOLD: int = 40

# 市場軸判準（測試 3）。`taiwan_stock_info.type` 的其餘值（`emerging`）與查不到的
# 代號都不列入，它們在日頻表裡佔比不到 1%，納入只會讓分母抖動
MARKETS: Tuple[str, str] = ("twse", "tpex")

# 「整個市場不見了」的門檻：實測壞掉的日子該市場只剩 0~15 檔而鄰日 1,020~1,339 檔，
# 10% 這條線落在兩個數量級的空隙裡，量少的正常日與壞掉的日子都不會擦到
MISSING_RATIO: float = 0.1
NEIGHBOUR_RADIUS: int = 5

# 鄰日中位數低於此值時不判定：市場剛開辦或資料稀疏的早年，分母本來就小
MIN_NEIGHBOUR_MEDIAN: int = 100

pytestmark = pytest.mark.slow


def load_dates(conn: sqlite3.Connection, table: str) -> Set[str]:
    """取得指定表在共同區間內的所有日期"""

    return {
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT date FROM {table} WHERE date <= ?", (COMMON_RANGE_END,)
        )
    }


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists() or not Path(TW_FUTURES_DB_PATH).exists(),
    reason="需要 tw_stock.db 與 tw_futures.db 才能交叉比對交易日",
)
def test_no_non_trading_day_batches() -> None:
    """沒有任何一張日頻表存在「只有它有」的日期

    市場開市與否對所有來源都一樣。某張表有、其他三張全無的日期，
    只可能是站方在休市日回了東西而清洗端照請求日期寫了進去。
    """

    stock_conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
    futures_conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        price: Set[str] = load_dates(stock_conn, PRICE_TABLE_NAME)
        chip: Set[str] = load_dates(stock_conn, CHIP_TABLE_NAME)
        margin: Set[str] = load_dates(stock_conn, MARGIN_TABLE_NAME)
        large_trader: Set[str] = load_dates(futures_conn, "futures_large_trader")
    finally:
        stock_conn.close()
        futures_conn.close()

    assert not (chip - price - margin - large_trader), (
        f"`{CHIP_TABLE_NAME}` 有其他三張表都沒有的日期："
        f"{sorted(chip - price - margin - large_trader)}"
    )
    assert not (margin - price - chip - large_trader), (
        f"`{MARGIN_TABLE_NAME}` 有其他三張表都沒有的日期："
        f"{sorted(margin - price - chip - large_trader)}"
    )
    assert not (price - chip - margin - large_trader), (
        f"`{PRICE_TABLE_NAME}` 有其他三張表都沒有的日期："
        f"{sorted(price - chip - margin - large_trader)}"
    )


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(),
    reason="需要 tw_stock.db 才能比對籌碼內容指紋",
)
def test_no_stale_replayed_batches() -> None:
    """沒有兩個日期共用同一份籌碼內容

    站方在某些請求下會回一份**過期頁**，清洗端蓋上請求日期後，同一批數字就會
    出現在好幾天。實際發生過：2017-12-18 的 TWSE 內容被複製到另外 11 個日期，
    其中 5 天還是真實交易日（真正的資料因此沒進來）。

    以每日「絕對值最大的 50 筆 `(stock_id, 三大法人買賣超股數)`」為指紋——
    真實行情不可能有兩天的前 50 大完全一致。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
    try:
        rows: List[Tuple[str, str, int]] = conn.execute(
            f"SELECT date, stock_id, 三大法人買賣超股數 FROM {CHIP_TABLE_NAME} "
            f"WHERE 三大法人買賣超股數 <> 0"
        ).fetchall()
    finally:
        conn.close()

    by_date: Dict[str, List[Tuple[int, str, int]]] = {}
    for date, stock_id, value in rows:
        by_date.setdefault(date, []).append((abs(value), stock_id, value))

    fingerprints: Dict[str, List[str]] = {}
    for date, entries in by_date.items():
        top: List[Tuple[int, str, int]] = sorted(entries, reverse=True)[
            :FINGERPRINT_SIZE
        ]
        digest: str = hashlib.md5(
            str([(stock_id, value) for _, stock_id, value in top]).encode()
        ).hexdigest()
        fingerprints.setdefault(digest, []).append(date)

    collisions: List[List[str]] = [
        sorted(dates) for dates in fingerprints.values() if len(dates) > 1
    ]

    assert not collisions, f"以下日期共用同一份籌碼內容（過期頁被重播）：{collisions}"


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(),
    reason="需要 tw_stock.db 才能比對行情內容指紋",
)
def test_no_stale_replayed_price_batches() -> None:
    """沒有兩個日期共用同一份行情內容

    與籌碼那條同源，但**壞法更隱蔽**：`price` 是交易日曆與還原價的來源，
    被重播的那天整批收盤價都是別天的，而價格看起來完全合理。實際發生過：
    `2020-04-14` 的 TWSE 半邊是 `2020-12-18` 的重播，2330 兩天皆為開 508、
    收 510（前後兩日分別是 278.5 與 287.5），2,007 列中有 1,129 列完全相同。

    **判準是「重疊筆數」而不是整份指紋的雜湊**：被重播的往往只有半個市場，
    另外半邊是當天真實的資料，整份雜湊因此不會碰撞——以修正前的資料實測，
    兩天的前 50 大有 47 筆完全相同，雜湊卻不同。改看重疊筆數就抓得到。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
    try:
        rows: List[Tuple[str, str, float, int]] = conn.execute(
            f"SELECT date, stock_id, 收盤價, 成交股數 FROM {PRICE_TABLE_NAME} "
            f"WHERE 成交股數 > 0"
        ).fetchall()
    finally:
        conn.close()

    by_date: Dict[str, List[Tuple[int, str, float]]] = {}
    for date, stock_id, close, volume in rows:
        by_date.setdefault(date, []).append((volume, stock_id, close))

    # 每個「(個股, 收盤價, 成交股數)」對應到哪些日期；真實行情裡這種三元組
    # 幾乎不可能在兩天重複，重複就代表其中一天是另一天的重播
    dates_by_entry: Dict[Tuple[str, float, int], List[str]] = {}
    for date, entries in by_date.items():
        for volume, stock_id, close in sorted(entries, reverse=True)[:FINGERPRINT_SIZE]:
            dates_by_entry.setdefault((stock_id, close, volume), []).append(date)

    shared: Dict[Tuple[str, str], int] = {}
    for dates in dates_by_entry.values():
        for first, second in combinations(sorted(set(dates)), 2):
            shared[(first, second)] = shared.get((first, second), 0) + 1

    collisions: List[Tuple[str, str, int]] = sorted(
        (first, second, count)
        for (first, second), count in shared.items()
        if count >= REPLAY_OVERLAP_THRESHOLD
    )

    assert not collisions, (
        f"以下日期的前 {FINGERPRINT_SIZE} 大成交量內容高度重疊（過期頁被重播）："
        f"{collisions}"
    )


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(),
    reason="需要 tw_stock.db 才能比對代號與名稱",
)
def test_no_symbol_carries_two_names_on_the_same_day() -> None:
    """同一天、同一個代號不得出現兩個證券名稱

    這是「前導 0 被吃掉、變成另一個合法代號」唯一可靠的跡象。`pd.read_csv()`／
    `read_html()` 只要看到某份檔案的代號全是數字就整欄推斷成整數，六碼 ETF 少掉
    前兩個 0 之後剛好是 4 碼——`006201`（元大富櫃50）變成 `6201`，而 `6201`
    （亞弘電）本身是合法的上市代號。**代號長度、比對 `taiwan_stock_info` 都驗不出來**：
    長度是 4、代號也真的存在；`stock_info` 只有現名，ETF 改過名的更對不上。

    唯一會露出馬腳的地方是這裡：兩檔不同的證券擠在同一個 `(date, stock_id)` 底下。
    改名不會命中——那是跨時間的，同一天只會有一個名字。

    實測抓到 `006201`／`006202`／`006205`／`006206` 共 508 列（`chip` 506、`price` 2）。
    `margin` 不在範圍內：它的主鍵是 `(date, stock_id)`，冒名的那一列會被
    `INSERT OR IGNORE` 直接吞掉，表裡不會留下痕跡。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
    try:
        offenders: Dict[str, List[Tuple[str, str, int]]] = {}
        for table in (PRICE_TABLE_NAME, CHIP_TABLE_NAME):
            rows: List[Tuple[str, str, int]] = conn.execute(
                f"""
                SELECT date, stock_id, COUNT(DISTINCT 證券名稱) AS names
                FROM {table}
                GROUP BY date, stock_id
                HAVING names > 1
                ORDER BY date
                """
            ).fetchall()
            if rows:
                offenders[table] = rows
    finally:
        conn.close()

    assert not offenders, (
        "以下 (日期, 代號) 底下有兩個以上的證券名稱，多半是前導 0 被吃掉："
        f"{ {table: rows[:5] for table, rows in offenders.items()} }"
    )


@pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(),
    reason="需要 tw_stock.db 才能比對逐日的市場別分布",
)
def test_no_single_market_batches() -> None:
    """沒有任何一天只入庫了半個市場

    台股的日頻表都是**兩個來源拼成一天**：TWSE 一份、TPEX 一份，清洗後寫進
    同一張表。若其中一個來源當天沒問到，另一個仍會照常入庫——那一天在庫裡
    看起來是有資料的，只是少了半個市場。

    **這一條補的正是上面兩條的盲區。** 日期軸正常（四張表都有那天），
    內容也是真的（指紋不碰撞），缺口統計更不會出聲（它看的是「哪幾天完全沒有」）。
    2026-09-05 掃描抓到 11 天：`chip` 9 天、`price` 2 天，全部缺 TWSE 那半邊。

    判準刻意用「低於鄰日中位數的 10%」而不是第一次清理時的 85%：後者會連帶掃到
    列數本來就偏低的日期（早年補行交易的週六、跌停鎖死的股災日），變成紅燈噪音。
    實測命中的 11 天該市場只剩 0~15 檔而鄰日 1,020~1,339 檔，兩者之間有兩個
    數量級的空隙，10% 這條線落在空隙裡，兩邊都不會擦到。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_STOCK_DB_PATH)
    try:
        market_map: Dict[str, str] = {}
        for stock_id, market in conn.execute(
            f"SELECT stock_id, type FROM {STOCK_INFO_TABLE_NAME}"
        ):
            market_map.setdefault(stock_id, market)

        offenders: List[str] = []
        for table in (PRICE_TABLE_NAME, CHIP_TABLE_NAME, MARGIN_TABLE_NAME):
            per_date: Dict[str, Dict[str, int]] = {}
            for date, stock_id in conn.execute(f"SELECT date, stock_id FROM {table}"):
                counts: Dict[str, int] = per_date.setdefault(
                    date, {name: 0 for name in MARKETS}
                )
                market_of_stock: str = market_map.get(stock_id, "")
                if market_of_stock in counts:
                    counts[market_of_stock] += 1

            dates: List[str] = sorted(per_date)
            for i, date in enumerate(dates):
                lo: int = max(0, i - NEIGHBOUR_RADIUS)
                hi: int = min(len(dates), i + NEIGHBOUR_RADIUS + 1)
                neighbours: List[str] = [dates[j] for j in range(lo, hi) if j != i]
                if not neighbours:
                    continue

                for market in MARKETS:
                    median: float = statistics.median(
                        [per_date[d][market] for d in neighbours]
                    )
                    if median < MIN_NEIGHBOUR_MEDIAN:
                        continue
                    if per_date[date][market] < MISSING_RATIO * median:
                        offenders.append(
                            f"{table} {date} {market} 僅 {per_date[date][market]} 檔"
                            f"（鄰日中位數 {int(median)}）"
                        )
    finally:
        conn.close()

    assert not offenders, f"以下日期只入庫了半個市場：{offenders}"
