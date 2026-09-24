import datetime
from typing import List, Optional, Tuple

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.futures_tick_cleaner import FuturesTickCleaner
from core.pipeline.tw.crawlers.futures_tick_crawler import FuturesTickCrawler
from core.pipeline.tw.updaters.futures_tick_updater import FuturesTickUpdater
from core.utils import SHIOAJI_FUTURES_CATEGORY

"""
台期貨 Tick ETL 測試

**本組最容易錯的是「兩邊的契約代碼不一樣」**：小型臺指在 TAIFEX 是 `MTX`、
在 Shioaji 是 `MXF`；電子期貨 `TE` vs `EXF`；金融期貨 `TF` vs `FXF`；
微型臺指與兩檔小型契約則兩邊同名——**沒有規律**。猜錯的症狀是「查無此契約」，
一整段回補靜靜地什麼都沒抓到。對照表是 2026-09-02 實際登入 Shioaji 逐一核對的。

第二個重點是**時段要由時間戳判定**：Shioaji 回的是整個交易日的逐筆，
**含前一日 15:00 開始的夜盤**。不標時段的話，日盤策略會吃到夜盤成交而不自知。
實測 2026-08-28 的 TX202612：29 筆裡有 10 筆的時間戳是 08-27 晚上。
"""

DATE: datetime.date = datetime.date(2026, 8, 28)


def make_raw_ticks() -> pd.DataFrame:
    """模擬 Shioaji 回傳的原始 ticks（含夜盤、非交易時段與壞時間戳）"""

    return pd.DataFrame(
        {
            "ts": [
                "2026-08-27 19:21:52.549",  # 前一日夜盤（屬 8/28 交易日）
                "2026-08-28 09:00:01.000",  # 日盤
                "2026-08-28 14:00:00.000",  # 非交易時段
                "not-a-timestamp",
            ],
            "close": [46706.0, 46800.0, 46810.0, 1.0],
            "volume": [1, 2, 3, 4],
            "bid_price": [46706.0, 46799.0, 46809.0, 1.0],
            "bid_volume": [1, 1, 1, 1],
            "ask_price": [46899.0, 46801.0, 46811.0, 1.0],
            "ask_volume": [1, 1, 1, 1],
            "tick_type": [2, 1, 1, 1],
        }
    )


# === 契約代碼對照 ===
def test_shioaji_category_mapping_is_not_derivable() -> None:
    """
    **兩邊的代碼沒有規律**，只能逐一對照

    加個 F 的規則會在 MTX（→ MXF 不是 MTXF）、TE（→ EXF）、TF（→ FXF）三處錯掉。
    """

    assert SHIOAJI_FUTURES_CATEGORY["TX"] == "TXF"
    assert SHIOAJI_FUTURES_CATEGORY["MTX"] == "MXF"
    assert SHIOAJI_FUTURES_CATEGORY["TE"] == "EXF"
    assert SHIOAJI_FUTURES_CATEGORY["TF"] == "FXF"
    # 這三檔兩邊同名——同樣不是規律，是查證的結果
    assert SHIOAJI_FUTURES_CATEGORY["TMF"] == "TMF"
    assert SHIOAJI_FUTURES_CATEGORY["ZEF"] == "ZEF"
    assert SHIOAJI_FUTURES_CATEGORY["ZFF"] == "ZFF"


def test_symbol_uses_year_month_not_letter_code() -> None:
    """
    用 `symbol`（`TXF202609`）不用 `code`（`TXFI6`）

    `code` 是「月份字母 ＋ 年末碼」，字母碼每 10 年重複一次，跨年回補會取到
    錯誤年份的契約。
    """

    assert FuturesTickCrawler.to_shioaji_symbol("TX", "202609") == "TXF202609"
    assert FuturesTickCrawler.to_shioaji_symbol("MTX", "202612") == "MXF202612"


def test_unmapped_product_returns_none() -> None:
    """沒對照過的商品回 None 並記 warning——**不可自己拼一個**"""

    assert FuturesTickCrawler.to_shioaji_symbol("XIF", "202609") is None


def test_weekly_contract_is_rejected() -> None:
    """
    週契約在 Shioaji 是**獨立分類**（MX1／MX2…），不是同分類的不同到期月

    硬拼會得到一個不存在的 symbol，症狀同樣是「查無此契約」。
    """

    assert FuturesTickCrawler.to_shioaji_symbol("TX", "202609W1") is None


# === 清洗 ===
def test_session_is_resolved_from_the_timestamp() -> None:
    """
    **時段由時間戳判定**：Shioaji 回的整個交易日含前一日夜盤

    不標的話，日盤策略會吃到夜盤成交而不自知（夜盤的量能與價格行為差很多）。
    """

    cleaned: pd.DataFrame = FuturesTickCleaner().clean(make_raw_ticks(), "TX", "202612")

    sessions: List[Optional[str]] = list(cleaned["session"])
    assert sessions[0] == "night"  # 前一日 19:21
    assert sessions[1] == "day"  # 當日 09:00
    # **`pd.isna()` 不是 `is None`**：pandas 會把 object 欄的 None 正規化成 NaN，
    # 用 `is None` 斷言永遠會失敗（本專案在 loader 那邊也踩過同一個坑）
    assert pd.isna(sessions[2])  # 13:45~15:00 之間，理論上不該有成交


def test_invalid_timestamps_are_dropped() -> None:
    """
    時間戳是 tick 唯一的排序依據，壞掉就只能丟

    補值或猜測都會讓成交順序錯亂，而順序錯了整段回測都不能看。
    """

    cleaned: pd.DataFrame = FuturesTickCleaner().clean(make_raw_ticks(), "TX", "202612")

    assert len(cleaned) == 3
    assert cleaned["time"].notna().all()


def test_contract_identity_is_split_into_two_columns() -> None:
    """識別欄拆成 `product` ／ `expiry`，與 `futures_price_daily` 的主鍵一致"""

    cleaned: pd.DataFrame = FuturesTickCleaner().clean(make_raw_ticks(), "TX", "202612")

    assert set(cleaned["product"]) == {"TX"}
    assert set(cleaned["expiry"]) == {"202612"}
    assert list(cleaned.columns) == FuturesTickCleaner.COLUMNS


def test_empty_input_returns_none() -> None:
    """沒有資料時回 None，不要回一張空表讓下游誤以為「有查到但都是 0」"""

    cleaner: FuturesTickCleaner = FuturesTickCleaner()

    assert cleaner.clean(pd.DataFrame(), "TX", "202612") is None
    assert cleaner.clean(None, "TX", "202612") is None


# === 契約清單與配額 ===
def test_near_month_filter_keeps_the_earliest_expiry() -> None:
    """
    預設只爬近月：期貨的量集中在近月，遠月一天可能只有幾百筆卻同樣佔配額

    實測 2026-08-28 的 TX 遠月（202612）整天只有 29 筆。
    """

    contracts: List[Tuple[str, str]] = [
        ("TX", "202612"),
        ("TX", "202609"),
        ("MTX", "202610"),
    ]

    assert FuturesTickUpdater.keep_near_month(contracts) == [
        ("MTX", "202610"),
        ("TX", "202609"),
    ]


class StubUsage:
    """假的配額回應"""

    def __init__(self, remaining_mb: float) -> None:
        self.remaining_bytes = int(remaining_mb * 1024**2)


class StubShioaji:
    """只實作 `usage()` 的假 API"""

    def __init__(self, remaining_mb: Optional[float]) -> None:
        self.remaining_mb = remaining_mb

    def usage(self):
        if self.remaining_mb is None:
            raise RuntimeError("usage unavailable")
        return StubUsage(self.remaining_mb)


def test_quota_guard_stops_before_the_limit() -> None:
    """
    配額不足就停手，**不是繼續硬爬**

    用完之後的請求會直接失敗，繼續爬只是白白花時間。
    """

    updater: FuturesTickUpdater = FuturesTickUpdater.__new__(FuturesTickUpdater)

    assert updater.check_quota(StubShioaji(500.0)) is True
    assert updater.check_quota(StubShioaji(10.0)) is False


def test_quota_check_passes_when_usage_is_unavailable() -> None:
    """查不到用量時放行——那是 API 暫時異常，不該因此停掉整段回補"""

    updater: FuturesTickUpdater = FuturesTickUpdater.__new__(FuturesTickUpdater)

    assert updater.check_quota(StubShioaji(None)) is True


# === 沒有 DolphinDB 的環境 ===
def test_loader_degrades_without_dolphindb(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    **沒有 DolphinDB 也不能整個壞掉**

    期貨 tick 是選用功能（`[tick]` 相依），只跑日線回測的機器與 CI 都沒有它。
    此時應保留中繼檔並記 warning，而不是拋錯中止。

    **`TICK_DB_PATH` 由測試自己設**：它是「設定」而不是「DolphinDB 在不在」，缺值本來就該
    當場拋出（見 `test_require_tick_db_path_raises_when_unset`）。不設的話本測試會在
    `.env` 有值的本機通過、在沒有 `.env` 的 CI 失敗——量到的是機器，不是降級行為。
    """

    import core.config.schema as schema
    from core.pipeline.tw.loaders.futures_tick_loader import FuturesTickLoader

    monkeypatch.setattr(schema, "TICK_DB_PATH", "dfs://tickDB")
    # 裝了 dolphindb 但 server 沒開的機器會逐次重試；只試一次、不等待
    monkeypatch.setattr(FuturesTickLoader, "CONNECT_MAX_RETRIES", 1)
    monkeypatch.setattr(FuturesTickLoader, "CONNECT_RETRY_DELAY", 0.0)

    loader: FuturesTickLoader = FuturesTickLoader()

    assert loader.add_to_db(pd.DataFrame([{"product": "TX"}])) == 0
