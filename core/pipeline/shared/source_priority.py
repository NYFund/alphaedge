from typing import Dict, List, Set, Tuple

import pandas as pd
from loguru import logger

"""
跨來源重複列的去重規則：以「來源優先序」決定勝出者

同一筆事件（`(date, stock_id)`）可能同時來自多個來源——除權息的 FinMind 回補與
TPEX 日更、公司行動的交易所端點與行情偵測。**留下哪一筆必須由來源優先序決定，
不可依檔名字典序**：直接對 `sorted(dir.iterdir())` 的結果 `keep="last"` 的話，
勝出者取決於檔名的字母順序，日後多一個來源或改個檔名前綴，勝出的就換人，
而且不會有任何跡象。

這是清洗規則而不是資料存取，故放在 pipeline 而不放 DAO。
"""


def dedup_by_source_priority(
    df: pd.DataFrame,
    source_priority: List[str],
    label: str,
    key_columns: Tuple[str, ...] = ("date", "stock_id"),
) -> pd.DataFrame:
    """
    - Description:
        以來源優先序去重：同鍵多筆時保留優先序最高的那一筆

        `source_priority` 由低到高排列，清單中沒列到的來源優先序最低並發出警告
        （應補進清單）。同一優先序的多筆以出現順序最後一筆為準。
    - Parameters:
        - df: pd.DataFrame
            合併後的資料；以「資料來源」欄判斷來源
        - source_priority: List[str]
            來源名稱，由低到高
        - label: str
            日誌用的資料名稱（例如 `dividend`）
        - key_columns: Tuple[str, ...]
            去重的鍵
    - Return:
        - pd.DataFrame
            去重後的資料，依鍵排序、欄位順序不變
    """

    keys: List[str] = list(key_columns)

    if "資料來源" not in df.columns:
        logger.warning(f"[{label}] 資料沒有「資料來源」欄，退回以出現順序去重")
        return df.drop_duplicates(subset=keys, keep="last")

    rank_by_source: Dict[str, int] = {
        source: index for index, source in enumerate(source_priority, start=1)
    }
    # 清單中沒列到的來源 rank 記為 0，排序後落在最前面；
    # 去重取 `keep="last"`，故等同於優先序最低、同鍵時一定被蓋掉
    rank: pd.Series = df["資料來源"].map(rank_by_source).fillna(0)

    unknown: Set[str] = set(df.loc[rank == 0, "資料來源"].unique())
    if unknown:
        logger.warning(
            f"[{label}] 出現未列入優先序的來源 {sorted(unknown)}，"
            f"以最低優先序處理（同鍵時一定被其他來源蓋掉）；請補進 SOURCE_PRIORITY"
        )

    ordered: pd.DataFrame = df.assign(_rank=rank).sort_values("_rank", kind="stable")
    deduped: pd.DataFrame = ordered.drop_duplicates(subset=keys, keep="last")
    return deduped.drop(columns=["_rank"]).sort_values(keys, kind="stable")
