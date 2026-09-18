import datetime
import sqlite3
from pathlib import Path
from typing import List, Optional

import pandas as pd
import pytest

from core.config import (
    FUTURES_PRICE_DAILY_TABLE_NAME,
    FUTURES_PRODUCT_NIGHT_SESSION_START_DATES,
    TW_FUTURES_DB_PATH,
)
from core.pipeline.tw.updaters.futures_price_updater import FuturesPriceUpdater
from core.pipeline.utils.exceptions import ProductUpdateError
from core.utils import FuturesSession

"""
台期貨行情 Updater 測試：續跑起點、商品防呆、日期挑選

**逐商品續跑**是本檔的重點——各商品上市日不同且會陸續加入設定檔，
若以全表最新日當起點，新商品的歷史會整段補不到且不會有任何錯誤訊息。
不連網路（crawler 以 stub 取代）、不碰正式的 tw_futures.db。
"""

DATE: datetime.date = datetime.date(2026, 8, 27)


@pytest.fixture
def updater(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FuturesPriceUpdater:
    """Updater fixture，DB 與 downloads 目錄都改為暫存"""

    db_path: Path = tmp_path / "tw_futures.db"
    monkeypatch.setattr(
        "core.pipeline.tw.updaters.futures_price_updater.TW_FUTURES_DB_PATH", db_path
    )
    monkeypatch.setattr(
        "core.pipeline.tw.loaders.futures_price_loader.TW_FUTURES_DB_PATH", db_path
    )

    futures_price_updater: FuturesPriceUpdater = FuturesPriceUpdater()
    futures_price_updater.loader.futures_price_dir = tmp_path / "price"
    futures_price_updater.loader.futures_price_dir.mkdir(parents=True, exist_ok=True)
    futures_price_updater.cleaner.futures_price_dir = (
        futures_price_updater.loader.futures_price_dir
    )
    # 空產出重試的等待在測試中一律歸零，否則每個空日都要真的睡 60 秒
    futures_price_updater.EMPTY_RETRY_DELAY_SECONDS = 0
    return futures_price_updater


def day_session_raw() -> pd.DataFrame:
    """TX 日盤一列原始行情（欄位順序同 TAIFEX 頁面）"""

    return pd.DataFrame(
        [
            [
                "TX",
                "202609",
                46175,
                46517,
                46006,
                46078,
                "▲75",
                "▲0.16%",
                26057,
                50701,
                76758,
                46064,
                104881,
                46077,
                46088,
                49651,
                24962,
            ]
        ]
    )


def insert_row(conn: sqlite3.Connection, date: str, product: str) -> None:
    """直接塞一列，用來製造「表內已有資料」的狀態"""

    conn.execute(
        f"INSERT OR IGNORE INTO {FUTURES_PRICE_DAILY_TABLE_NAME} "
        f'("date", product, expiry, session, 成交量) VALUES (?, ?, ?, ?, ?)',
        (date, product, "202609", "day", 1),
    )
    conn.commit()


# === 逐商品續跑 ===
def test_start_date_falls_back_to_default_when_no_data(
    updater: FuturesPriceUpdater,
) -> None:
    """表內沒有該商品時，起點為傳入的預設日"""

    default: datetime.date = datetime.date(1998, 7, 21)

    assert updater.get_actual_update_start_date("TX", default) == default


def test_start_date_resumes_from_latest(updater: FuturesPriceUpdater) -> None:
    """表內已有資料時，起點為最新日 +1"""

    insert_row(updater.conn, "2026-08-27", "TX")

    assert updater.get_actual_update_start_date(
        "TX", datetime.date(1998, 7, 21)
    ) == datetime.date(2026, 8, 28)


def test_resume_is_per_product(updater: FuturesPriceUpdater) -> None:
    """
    新加入的商品不可被既有商品的進度擋住

    TX 已補到 2026，此時把 MTX 加進設定檔，MTX 的起點仍須是預設日；
    若以全表最新日為準，MTX 的歷史會整段補不到且不會有任何錯誤訊息。
    """

    insert_row(updater.conn, "2026-08-27", "TX")
    default: datetime.date = datetime.date(1998, 7, 21)

    assert updater.get_actual_update_start_date("TX", default) == datetime.date(
        2026, 8, 28
    )
    assert updater.get_actual_update_start_date("MTX", default) == default


def test_backfill_ignores_table_progress(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    `resume=False` 一律照傳入的起日跑，不查表內進度

    起點要往**前**拉（例如把回補起點從 2015 改成 1998）時，日常路徑會被表內
    既有資料擋住而整段補不到，且只會顯示「已是最新」——沒有任何錯誤訊息。
    """

    insert_row(updater.conn, "2026-08-27", "TX")
    requested: list = []

    monkeypatch.setattr(
        updater.crawler,
        "crawl_futures_price",
        lambda date, product, session: requested.append(date) or None,
    )
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 99

    updater.update(
        start_date=datetime.date(2015, 1, 5),
        end_date=datetime.date(2015, 1, 6),
        products=["TX"],
        resume=False,
    )

    assert min(requested) == datetime.date(2015, 1, 5)
    assert max(requested) == datetime.date(2015, 1, 6)


def test_daily_update_still_resumes(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """預設路徑（`resume=True`）不受影響：表內已到 2026-08-27 就不重爬"""

    insert_row(updater.conn, "2026-08-27", "TX")
    requested: list = []

    monkeypatch.setattr(
        updater.crawler,
        "crawl_futures_price",
        lambda date, product, session: requested.append(date) or None,
    )
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())

    updater.update(
        start_date=datetime.date(2015, 1, 5),
        end_date=datetime.date(2026, 8, 27),
        products=["TX"],
    )

    assert requested == []


def test_empty_day_is_retried_before_being_counted_as_no_data(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    查無資料要再試一次才算數

    TAIFEX 擋流量時回的是 HTTP 200 ＋ 沒有行情表的頁面，crawler 看到的與「非交易日」
    一模一樣。2026-09-01 的回補實測就是這樣：連跑約 160 次請求後被擋，20 個有資料的
    交易日被判定成查無資料而誤觸保險絲中止。
    """

    attempts: dict = {}

    def flaky_crawl(date, product, session):
        """第一次一律空手，第二次才給資料——模擬「被擋 → 恢復」"""

        attempts[date] = attempts.get(date, 0) + 1
        if attempts[date] <= len(FuturesSession.data_sessions()):
            return None
        return pd.DataFrame({"契約": ["TX"]})

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", flaky_crawl)
    monkeypatch.setattr(
        updater.cleaner,
        "clean_futures_price",
        lambda df, *_: pd.DataFrame({"date": ["2026-08-27"]}),
    )
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    monkeypatch.setattr(updater, "load_batch", lambda *_: None)
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 1

    # 保險絲設為 1：沒有重試機制的話，第一個空日就會中止
    updater.update(start_date=DATE, end_date=DATE, products=["TX"], resume=False)

    assert attempts[DATE] > len(FuturesSession.data_sessions())


# === 商品防呆 ===
def test_update_rejects_malformed_product(updater: FuturesPriceUpdater) -> None:
    """明顯不合法的代碼在送出任何請求之前就擋下"""

    with pytest.raises(ValueError, match="格式不正確"):
        updater.update(start_date=DATE, end_date=DATE, products=["tx-1"])


def test_aborts_when_product_yields_nothing(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    連續多日查無資料要中止，不可跑完數千次請求才發現整張表是空的

    這是「代碼拼錯」的真正防線——crawler 只擋格式，因為合法代碼沒有可靠的
    靜態清單（TAIFEX 的下拉內容不穩定）。拼錯的代碼會安靜地每天查無資料，
    看起來就像「這幾年一直都是假日」。
    """

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", lambda *_: None)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 3

    # 保險絲的原始訊息要原封不動帶到 `update()` 最後拋出的例外裡
    with pytest.raises(ProductUpdateError, match="連續 3 個候選日皆無資料"):
        updater.update(
            start_date=datetime.date(2026, 8, 3),
            end_date=datetime.date(2026, 8, 14),
            products=["TXX"],
        )


def test_does_not_abort_when_data_resumes(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    中間穿插的無資料日（國定假日）不可觸發中止

    保險絲算的是**連續**空產出，有資料就歸零；否則連假會被誤判成代碼錯誤。
    """

    calls: List[int] = []

    def fake_crawl(date, product, session):
        calls.append(1)
        # 前兩個交易日放假，之後恢復
        if date <= datetime.date(2026, 8, 4):
            return None
        return day_session_raw() if session == FuturesSession.DAY else None

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", fake_crawl)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 3

    updater.update(
        start_date=datetime.date(2026, 8, 3),
        end_date=datetime.date(2026, 8, 7),
        products=["TX"],
    )

    conn = sqlite3.connect(updater.loader.futures_price_dir.parent / "tw_futures.db")
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM {FUTURES_PRICE_DAILY_TABLE_NAME}"
        ).fetchone()[0]
        > 0
    )


def test_one_failing_product_does_not_block_the_rest(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    一個商品觸發保險絲，排在後面的商品仍要更新；但跑完之後整體要以失敗結束

    股期一次更新流動性前 N 檔，上市晚於回補起點的那檔會觸發保險絲——不隔離的話
    一檔中止整批，排在後面的商品全部不更新。
    """

    def fake_crawl(date, product, session):
        if product == "TXX":
            return None
        return day_session_raw() if session == FuturesSession.DAY else None

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", fake_crawl)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 3

    with pytest.raises(ProductUpdateError) as exc_info:
        updater.update(
            start_date=datetime.date(2026, 8, 3),
            end_date=datetime.date(2026, 8, 7),
            products=["TXX", "TX"],
        )

    assert list(exc_info.value.failures) == ["TXX"]
    assert exc_info.value.succeeded == 1
    conn = sqlite3.connect(updater.loader.futures_price_dir.parent / "tw_futures.db")
    assert (
        conn.execute(
            f"SELECT COUNT(*) FROM {FUTURES_PRICE_DAILY_TABLE_NAME} WHERE product = 'TX'"
        ).fetchone()[0]
        > 0
    )


# === 日期挑選 ===
def test_weekends_are_skipped(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """週末不送出請求；2026-08-22／23 為週六日"""

    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())

    dates: List[datetime.date] = updater.get_candidate_dates(
        datetime.date(2026, 8, 21), datetime.date(2026, 8, 24)
    )

    assert dates == [datetime.date(2026, 8, 21), datetime.date(2026, 8, 24)]


def test_traded_weekend_is_included(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """補行交易日（開市的週末）必須納入，否則那天整天缺資料"""

    traded: datetime.date = datetime.date(2026, 8, 22)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: {traded})

    dates: List[datetime.date] = updater.get_candidate_dates(
        datetime.date(2026, 8, 21), datetime.date(2026, 8, 24)
    )

    assert traded in dates


# === 串接 ===
def test_update_writes_rows_end_to_end(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """crawler 以 stub 取代，驗證 clean → load 的串接與 session 標記"""

    day_raw = pd.DataFrame(
        [
            [
                "TX",
                "202609",
                46175,
                46517,
                46006,
                46078,
                "▲75",
                "▲0.16%",
                26057,
                50701,
                76758,
                46064,
                104881,
                46077,
                46088,
                49651,
                24962,
            ]
        ]
    )
    night_raw = pd.DataFrame(
        [
            [
                "TX",
                "202609",
                46002,
                46142,
                45766,
                45993,
                "▼-10",
                "▼-0.02%",
                26057,
                "-",
                "-",
                45983,
                45993,
                49651,
                24962,
            ]
        ]
    )

    def fake_crawl(
        date: datetime.date, product: str, session: FuturesSession
    ) -> Optional[pd.DataFrame]:
        return day_raw.copy() if session == FuturesSession.DAY else night_raw.copy()

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", fake_crawl)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0

    updater.update(start_date=DATE, end_date=DATE, products=["TX"])

    conn = sqlite3.connect(tmp_path / "tw_futures.db")
    rows = conn.execute(
        f"SELECT session, 結算價 FROM {FUTURES_PRICE_DAILY_TABLE_NAME} ORDER BY session"
    ).fetchall()

    assert rows == [("day", 46064.0), ("night", None)]


def test_start_date_is_clamped_to_listing_date(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    上市較晚的商品，起點會被夾到上市日

    呼叫端通常對所有商品傳同一個 `start_date`。若照傳入值去爬，TMF 在 2024-07-29
    之前的每一天都查無資料，累積 20 天就觸發保險絲中止整檔回補——2026-09-01 的
    回補就是這樣停在 TMF 的。
    """

    requested: list = []

    monkeypatch.setattr(
        updater.crawler,
        "crawl_futures_price",
        lambda date, product, session: requested.append(date) or None,
    )
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 99

    updater.update(
        start_date=datetime.date(2015, 1, 5),
        end_date=datetime.date(2024, 7, 31),
        products=["TMF"],
        resume=False,
    )

    assert min(requested) == datetime.date(2024, 7, 29)


def test_unlisted_product_start_date_is_unchanged(updater: FuturesPriceUpdater) -> None:
    """股期等未登錄上市日的商品維持原起點——夾錯方向會靜默跳過資料"""

    assert updater.clamp_to_listing_date(
        "CDF", datetime.date(2015, 1, 5)
    ) == datetime.date(2015, 1, 5)


def test_start_date_after_listing_is_unchanged(updater: FuturesPriceUpdater) -> None:
    """日常續跑的起點已在上市日之後，不可被往前拉回上市日"""

    assert updater.clamp_to_listing_date(
        "TMF", datetime.date(2026, 1, 1)
    ) == datetime.date(2026, 1, 1)


# === 時段完整性 ===
def test_night_only_day_is_not_loaded(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    只拿到夜盤的那天整天不入庫

    續跑起點是「表內該商品最新日 +1」，入庫會讓它越過這一天，缺的日盤永遠不會
    再被請求——MTX 2026-09-02 即如此（夜盤 CSV 的寫入時間換算台北是當天 11:15，
    日盤根本還沒收盤）。
    """

    loaded: List[List[str]] = []

    def night_only(
        date: datetime.date, product: str, session: FuturesSession
    ) -> Optional[pd.DataFrame]:
        return None if session is FuturesSession.DAY else day_session_raw()

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", night_only)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    monkeypatch.setattr(updater, "load_batch", lambda dates: loaded.append(list(dates)))
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0
    updater.EMPTY_PRODUCT_ABORT_THRESHOLD = 99

    updater.update(start_date=DATE, end_date=DATE, products=["TX"], resume=False)

    assert loaded == []


def test_day_without_night_is_still_loaded(
    updater: FuturesPriceUpdater, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    只有日盤、夜盤沒有成交的那天照常入庫

    **不可要求兩段齊全**：TF、TE、ZFF 這類商品的夜盤本來就常常整天沒有成交
    （2017-05-15 之後 TF 有 1,977 天、TE 有 381 天只有日盤），要求齊全會讓它們
    永遠記不成完成、每次執行都重問一次。
    """

    loaded: List[List[str]] = []

    def day_only(
        date: datetime.date, product: str, session: FuturesSession
    ) -> Optional[pd.DataFrame]:
        return day_session_raw() if session is FuturesSession.DAY else None

    monkeypatch.setattr(updater.crawler, "crawl_futures_price", day_only)
    monkeypatch.setattr(updater, "get_traded_weekend_dates", lambda *_: set())
    monkeypatch.setattr(updater, "load_batch", lambda dates: loaded.append(list(dates)))
    updater.BATCH_RANDOM_DELAY_MIN = 0
    updater.BATCH_RANDOM_DELAY_MAX = 0

    updater.update(start_date=DATE, end_date=DATE, products=["TF"], resume=False)

    assert loaded == [["20260827"]]


@pytest.mark.slow
@pytest.mark.skipif(
    not Path(TW_FUTURES_DB_PATH).exists(), reason="需要 tw_futures.db 才能盤點"
)
def test_no_night_only_trading_days_in_table() -> None:
    """
    全表盤點：不得有「只有夜盤、沒有日盤」的交易日

    這種缺口不會有任何錯誤——續跑起點越過那天之後，缺的日盤就只能靠人工發現。
    夜盤制度 2017-05-15 晚上才上線，之前的日期不列入。
    """

    conn: sqlite3.Connection = sqlite3.connect(f"file:{TW_FUTURES_DB_PATH}?mode=ro")
    try:
        rows: List[tuple] = conn.execute(
            f"""
            SELECT product, date FROM (
                SELECT date, product,
                       MAX(session = 'day') AS has_day,
                       MAX(session = 'night') AS has_night
                FROM {FUTURES_PRICE_DAILY_TABLE_NAME}
                WHERE date > '2017-05-15'
                GROUP BY date, product
            )
            WHERE has_day = 0 AND has_night = 1
            ORDER BY product, date
            """
        ).fetchall()
    finally:
        conn.close()

    assert rows == []


# === 夜盤起始日 ===
def test_day_session_is_never_skipped() -> None:
    """日盤一律要查——本檢查只針對夜盤"""

    assert FuturesPriceUpdater.has_night_session(
        "TX", datetime.date(2015, 1, 5), FuturesSession.DAY
    )


def test_night_session_is_skipped_before_it_existed() -> None:
    """
    盤後交易上線前不查夜盤

    回補 2015~2017 那段等於白打一半的請求，並產生大量
    `No valid futures price rows` warning——資料是對的，雜訊是多的。
    """

    assert not FuturesPriceUpdater.has_night_session(
        "TX", datetime.date(2016, 6, 1), FuturesSession.NIGHT
    )
    # 起始日當天要查（夜盤第一天就有行情）
    assert FuturesPriceUpdater.has_night_session(
        "TX", datetime.date(2017, 5, 16), FuturesSession.NIGHT
    )


def test_night_session_start_is_per_product() -> None:
    """
    各商品是**逐批**納入盤後交易的，不是同一天全開

    用 TX 的日期一體適用會讓 TE（2018-11）、TF（2025-06）那幾年照樣白打請求。
    """

    date: datetime.date = datetime.date(2019, 1, 2)

    assert FuturesPriceUpdater.has_night_session("TE", date, FuturesSession.NIGHT)
    assert not FuturesPriceUpdater.has_night_session("TF", date, FuturesSession.NIGHT)


def test_unregistered_product_still_queries_the_night_session() -> None:
    """
    沒登錄起始日的商品（例如股期）照查不誤

    填一個過晚的日期會讓回補靜默跳過開頭幾天，比多打請求嚴重得多，
    所以寧可不登錄——不登錄只是回到本檢查加入前的行為。
    """

    assert FuturesPriceUpdater.has_night_session(
        "CDF", datetime.date(2015, 1, 5), FuturesSession.NIGHT
    )


@pytest.mark.skipif(
    not TW_FUTURES_DB_PATH.exists(), reason="需要 data/db/tw_futures.db"
)
def test_night_session_start_dates_are_not_later_than_the_data() -> None:
    """
    常數表登錄的起始日不可**晚於**表內實際最早的夜盤行情

    這張表是觀測值不是制度公告：它由現有資料推得，所以往前回補之後可能發現
    更早的夜盤行情。那時本測試會變紅，提醒把常數往前調——**填得太晚的後果是
    回補靜默跳過開頭幾天**，比多打請求嚴重得多。
    """

    conn: sqlite3.Connection = sqlite3.connect(TW_FUTURES_DB_PATH)
    try:
        rows: List[tuple] = conn.execute(
            f"SELECT product, MIN(date) FROM {FUTURES_PRICE_DAILY_TABLE_NAME} "
            f"WHERE session = ? GROUP BY product",
            (FuturesSession.NIGHT.value,),
        ).fetchall()
    finally:
        conn.close()

    observed: dict = {
        product: datetime.date.fromisoformat(str(earliest))
        for product, earliest in rows
        if earliest
    }

    too_late: List[str] = [
        f"{product}：常數 {start}、資料最早 {observed[product]}"
        for product, start in FUTURES_PRODUCT_NIGHT_SESSION_START_DATES.items()
        if product in observed and start > observed[product]
    ]
    assert not too_late, f"夜盤起始日登錄得比實際資料晚，回補會跳過開頭：{too_late}"
