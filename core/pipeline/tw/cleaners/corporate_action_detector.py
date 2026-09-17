import datetime
from typing import Dict, List, Optional, Set, Tuple, Union

import numpy as np
import pandas as pd
from loguru import logger

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, connect_sqlite
from core.dao.tw.corporate_action_dao import CorporateActionDAO
from core.dao.tw.stock_dividend_dao import StockDividendDAO
from core.dao.tw.stock_price_dao import StockPriceDAO

"""
由行情反推公司行動候選（`detected` 來源）

**為什麼需要這條路**：ETF 的受益權單位分割**不在任何結構化端點裡**。S1 查遍
`TWTB8U`（變更交易）與 TWSE OpenAPI 的 143 個端點都找不到 0050 在 2025-06-18
的一拆四；那類事件走的是公告系統而非可查詢的資料集。少了這條路，
最有名的那一筆分割就補不進來。

**判準只需要一個事實**：台股單日漲跌幅上限是 ±10%，所以非除權息日出現超過
15% 的單日變動，多半是公司行動或資料錯誤。這個判準不需要任何外部資料。

⚠️ **但槓桿與反向 ETF 是例外，它們的漲跌幅上限是 ±20%**（標的的兩倍）。
實測 2025-04-07 關稅重挫那天，00631L／00640L／00647L／00650L／00663L 等
一整批同時跌到 −20%（例如 00631L 由 200.30 到 160.25，倍率 0.80），
**那天它們都沒有任何公司行動**——單純是跌停。S1 的原始判準沒有考慮這一點。

因此本模組**不把它們濾掉，而是標記**（`疑似槓桿反向ETF` 欄）：直接濾掉會連
真正的反向分割一起藏起來，而 S1 正好就查到 00632R 在 2024-12-11 做過反向分割。
判讀時先看這一欄，再看停牌日數——公司行動幾乎都伴隨停止買賣數日，跌停不會。

**本模組只產出候選，不自動入庫**：倍率是從價格反推的近似值，而且「資料錯誤」
與「公司行動」在數字上長得一樣。要寫進資料表得由人確認後以 `detected` 來源
明確加入（見 `CorporateActionUpdater.load_detected_events()`）。
"""

# 台股單日漲跌幅上限 ±10%，超過這個門檻必然不是正常交易
DETECTION_THRESHOLD: float = 0.15

# **已確認為錯誤資料的日期**，一律排除，否則它們會淹掉真訊號。
#
# `2020-04-14` 整批是 `2020-12-18` 的內容：台積電當天顯示開 508／高 512／低 507／
# 收 510、成交股數 40,625,502，與 2020-12-18 **位元級相同**，而前後兩日分別是
# 278.5 與 287.5。這是「重播批次」的樣式：清洗端蓋的是請求參數的日期，而不是來源頁面內的日期
# （內容相同、日期不同的批次由 `tests/test_trading_calendar_guard.py` 盯住）。
# 該日 715 檔、次日 630 檔被判為跳空——**次日是被前一日的壞收盤拖累的**，
# 本身資料正確，故一併排除。
#
# ⚠️ 這是**資料本身要修**，不是護欄該長期容忍的事；修好後請把日期從這裡刪掉。
KNOWN_BAD_PRICE_DATES: Set[str] = {"2020-04-14", "2020-04-15"}


def detect_unexplained_moves(
    conn: Optional[DBConnection] = None,
    threshold: float = DETECTION_THRESHOLD,
    start_date: Optional[datetime.date] = None,
) -> pd.DataFrame:
    """
    - Description:
        找出「單日變動超過門檻、且無法由除權息或已知公司行動解釋」的日期

        這同時是 S4 護欄的判定式：事件表補齊後，這裡回傳的每一筆都應該要能
        被解釋掉；解釋不掉的就是新事件或資料錯誤。
    - Parameters:
        - conn: Optional[DBConnection]
            共用連線；未指定時自行以**唯讀**模式開啟
        - threshold: float
            單日變動的門檻（小數，預設 0.15）
        - start_date: Optional[datetime.date]
            只看這一天之後的資料；None 表示全期間
    - Return:
        - pd.DataFrame
            欄位為 `date`／`stock_id`／`前一日收盤`／`收盤價`／`推估倍率`／`停牌日數`
    """

    owns_conn: bool = conn is None
    if owns_conn:
        conn = connect_sqlite(TW_STOCK_DB_PATH, read_only=True)

    try:
        prices: pd.DataFrame = StockPriceDAO(conn=conn).get_close_prices(start_date)
        if prices.empty:
            return _empty_result()

        prices = prices[pd.to_numeric(prices["收盤價"], errors="coerce") > 0]
        prices["收盤價"] = prices["收盤價"].astype(float)
        prices = prices.sort_values(["stock_id", "date"], kind="stable")

        grouped = prices.groupby("stock_id", sort=False)
        prices["前一日收盤"] = grouped["收盤價"].shift(1)
        prices["前一交易日"] = grouped["date"].shift(1)
        prices = prices.dropna(subset=["前一日收盤"])

        prices["推估倍率"] = prices["收盤價"] / prices["前一日收盤"]
        candidates: pd.DataFrame = prices[
            (prices["推估倍率"] - 1).abs() > threshold
        ].copy()

        if candidates.empty:
            return _empty_result()

        bad: pd.Series = candidates["date"].astype(str).isin(KNOWN_BAD_PRICE_DATES)
        if bad.any():
            logger.info(f"[detector] 排除已知錯誤日期的 {int(bad.sum())} 筆候選")
            candidates = candidates[~bad]

        # 停牌日數：公司行動多半伴隨停止買賣，這是與「資料錯誤」的區別線索之一
        candidates["停牌日數"] = (
            pd.to_datetime(candidates["date"])
            - pd.to_datetime(candidates["前一交易日"])
        ).dt.days

        candidates = _drop_explained(conn, candidates)

        # 槓桿／反向 ETF 的代號以 L／R 結尾，其漲跌幅上限是 ±20%
        candidates["疑似槓桿反向ETF"] = (
            candidates["stock_id"].astype(str).str.upper().str.endswith(("L", "R"))
        )

        return candidates[
            [
                "date",
                "stock_id",
                "前一交易日",
                "前一日收盤",
                "收盤價",
                "推估倍率",
                "停牌日數",
                "疑似槓桿反向ETF",
            ]
        ].sort_values(["date", "stock_id"], kind="stable")
    finally:
        if owns_conn:
            conn.close()


def _empty_result() -> pd.DataFrame:
    """回傳欄位齊全的空表，讓呼叫端不必分辨「沒有候選」與「查不到資料」"""

    return pd.DataFrame(
        columns=[
            "date",
            "stock_id",
            "前一交易日",
            "前一日收盤",
            "收盤價",
            "推估倍率",
            "停牌日數",
            "疑似槓桿反向ETF",
        ]
    )


def _drop_explained(conn: DBConnection, candidates: pd.DataFrame) -> pd.DataFrame:
    """
    - Description:
        濾掉能被除權息或已知公司行動解釋的候選

        兩張表都以 `(date, stock_id)` 對齊。**`dividend` 表不存在時不濾**——
        那代表尚未跑過除權息 ETL，此時把候選全數保留才是誠實的（濾掉等於
        宣稱「已確認無關」）。
    - Parameters:
        - conn: DBConnection
            資料連線
        - candidates: pd.DataFrame
            尚未過濾的候選
    - Return:
        - pd.DataFrame
            過濾後的候選
    """

    event_daos: List[Tuple[Union[StockDividendDAO, CorporateActionDAO], str]] = [
        (StockDividendDAO(conn=conn), "除權息"),
        (CorporateActionDAO(conn=conn), "公司行動"),
    ]
    for dao, label in event_daos:
        if not dao.table_exists():
            logger.warning(f"[detector] {dao.TABLE_NAME} 不存在，不以{label}過濾候選")
            continue

        known: pd.DataFrame = dao.get_event_keys()
        if known.empty:
            continue

        before: int = len(candidates)
        candidates = _drop_events_within_gap(candidates, known)
        logger.info(f"[detector] 以{label}解釋掉 {before - len(candidates)} 筆候選")

    return candidates


def _drop_events_within_gap(
    candidates: pd.DataFrame, known: pd.DataFrame
) -> pd.DataFrame:
    """
    - Description:
        濾掉「停牌區間內有事件」的候選，而不是只比對同一天

        ⚠️ **不可用 `(date, stock_id)` 完全相等比對**：端點給的是官方
        `恢復買賣日期`，而該檔**實際有成交的第一天**可能更晚——遇到假日、
        或復牌當天無成交都會差開。實測 1529 樂事綠能的事件日是 2018-12-22
        （週六），價格跳空落在 2018-12-24；5455 的事件日 2014-12-30、
        跳空在 2015-01-07，差了 8 天。改成區間比對後，解釋不掉的由 361 筆
        降為 337 筆。

        判定式：事件日落在 **(前一個有成交日, 本次跳空日]** 之間，
        也就是「這段沒有交易的期間內發生了事件」。
    - Parameters:
        - candidates: pd.DataFrame
            含 `date`／`stock_id`／`前一交易日` 的候選
        - known: pd.DataFrame
            含 `date`／`stock_id` 的已知事件
    - Return:
        - pd.DataFrame
            仍解釋不掉的候選
    """

    events_by_id: Dict[str, np.ndarray] = {
        stock_id: pd.to_datetime(group["date"]).to_numpy()
        for stock_id, group in known.groupby(known["stock_id"].astype(str))
    }

    jump_dates: np.ndarray = pd.to_datetime(candidates["date"]).to_numpy()
    previous_dates: np.ndarray = pd.to_datetime(candidates["前一交易日"]).to_numpy()
    stock_ids: List[str] = candidates["stock_id"].astype(str).tolist()

    explained: List[bool] = []
    for stock_id, previous, jump in zip(stock_ids, previous_dates, jump_dates):
        events = events_by_id.get(stock_id)
        explained.append(
            False
            if events is None
            else bool(((events > previous) & (events <= jump)).any())
        )

    return candidates[~pd.Series(explained, index=candidates.index)]
