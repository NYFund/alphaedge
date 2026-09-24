from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import pytest

from core.config import CORPORATE_ACTION_TABLE_NAME, TW_STOCK_DB_PATH
from core.pipeline.tw.cleaners.corporate_action_detector import (
    detect_unexplained_moves,
)

"""
還原價序列的假跳空護欄

**事件表一定會過期**——每年都有新的減資與分割。沒有護欄的話，下一次漏掉某檔的
方式會與這次一模一樣：靜默、無錯誤、數字看起來合理，只有還原價序列上多一個
假跳空。

判準是一個不需要外部資料、也因此不會過期的事實：**台股單日漲跌幅上限 ±10%，
非除權息日出現超過 15% 的單日變動，正常交易做不到**。

⚠️ **但這個判準有三類例外**，寫護欄時必須排除，否則會被誤報淹掉：

| 例外 | 原因 | 規模 |
|------|------|:---:|
| 槓桿／反向 ETF | 漲跌幅上限是 ±20%，2025-04-07 重挫日整批跌停 | 153 筆 |
| 權證等 5 碼以上代號 | 無漲跌幅限制 | 248 筆 |
| 已知錯誤資料日期 | `2020-04-14` 整批是 `2020-12-18` 的內容 | 1,412 筆 |

排除之後仍有 337 筆一般個股／ETF 解釋不掉，多半是**不在減資端點裡的公司行動**
（合併換股、私募、股權轉換），以及零星的單日離群值（例如 2408 在 2015-12-14
由 38.0 跳到 52.9、隔日回到 42.5，那是資料錯誤不是事件）。那是事件表本身的涵蓋
範圍限制，不是本護欄的失效——所以本檔用**基準數**而不是「必須為 0」。
"""


pytestmark = pytest.mark.slow

_NEEDS_DB = pytest.mark.skipif(
    not Path(TW_STOCK_DB_PATH).exists(),
    reason="需要 tw_stock.db 才能掃描行情",
)

# 已知且必須被事件表解釋掉的跳空。**這是本護欄的核心**：
# 從 `corporate_action` 刪掉其中任一筆，對應的測試就會紅
KNOWN_EVENTS: List[Tuple[str, str, str]] = [
    # (股票代號, 跳空日, 說明)
    ("0050", "2025-06-18", "元大台灣50 一拆四；不在任何結構化端點，走 detected"),
    ("2408", "2014-09-09", "南亞科減資 8.10 → 80.93，全期間倍率最極端的一筆"),
    ("2371", "2025-06-23", "大同退還股款減資"),
]

# 解釋不掉的候選基準（2026-09-12 實測）。**只准降不准升**——
# 升代表出現了事件表沒收錄的新公司行動，那正是本護欄要抓的事。
#
# 刻意記總數而不是逐筆列出：337 筆逐一列進來會變成一份沒有人會維護的清單，
# 而且新事件與舊殘留混在一起反而看不出差別。
BASELINE_UNEXPLAINED_TOTAL: int = 585
BASELINE_UNEXPLAINED_ORDINARY: int = 337

# 基準數的寬限：行情表每天長新資料，偶發的單日離群值不該讓 CI 紅
TOLERANCE: int = 5


@pytest.fixture(scope="module")
def unexplained() -> pd.DataFrame:
    """全期間解釋不掉的跳空候選（掃一次給整個模組用）"""

    return detect_unexplained_moves()


def split_by_category(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """把候選分成三類，讓失敗訊息指得出是哪一類變多了"""

    leveraged: pd.Series = df["疑似槓桿反向ETF"].astype(bool)
    warrant: pd.Series = df["stock_id"].astype(str).str.len() > 5
    return {
        "槓桿反向ETF": df[leveraged],
        "權證等": df[~leveraged & warrant],
        "一般個股ETF": df[~leveraged & ~warrant],
    }


@_NEEDS_DB
@pytest.mark.parametrize(
    ("stock_id", "date", "note"),
    KNOWN_EVENTS,
    ids=[f"{item[0]}-{item[1]}" for item in KNOWN_EVENTS],
)
def test_known_corporate_action_is_explained(
    unexplained: pd.DataFrame, stock_id: str, date: str, note: str
) -> None:
    """
    已知的公司行動必須被事件表解釋掉

    **這是本護欄的核心測試**。把 `corporate_action` 裡 0050 那一筆刪掉，
    這條就會紅——已實測過。

    比對走「事件日落在停牌區間內」而不是完全相等：端點給的是官方
    `恢復買賣日期`，而該檔實際有成交的第一天可能因假日而更晚。
    """

    hit: pd.DataFrame = unexplained[
        (unexplained["stock_id"] == stock_id) & (unexplained["date"] == date)
    ]

    assert hit.empty, (
        f"{stock_id} 在 {date} 的跳空無法由 `{CORPORATE_ACTION_TABLE_NAME}` 解釋"
        f"（{note}）。事件表可能漏了這一筆，或日期對不上停牌區間。"
    )


@_NEEDS_DB
def test_unexplained_count_does_not_grow(unexplained: pd.DataFrame) -> None:
    """
    解釋不掉的跳空不得增加

    **升代表事件表過期了**：出現了新的減資或分割而沒有人跑 ETL。
    降是好事（補了資料或修了行情），此時請把基準數調低。
    """

    categories: Dict[str, pd.DataFrame] = split_by_category(unexplained)
    summary: str = "、".join(
        f"{name} {len(frame)} 筆" for name, frame in categories.items()
    )

    assert len(unexplained) <= BASELINE_UNEXPLAINED_TOTAL + TOLERANCE, (
        f"解釋不掉的跳空由 {BASELINE_UNEXPLAINED_TOTAL} 增為 {len(unexplained)} 筆"
        f"（{summary}）。請跑 `python -m tasks.update_db --target corporate_action`；"
        "若補完仍在，代表是不在減資端點裡的公司行動（合併換股、私募等），"
        "確認後調高基準數並註明原因。"
    )


@_NEEDS_DB
def test_ordinary_stocks_unexplained_does_not_grow(
    unexplained: pd.DataFrame,
) -> None:
    """
    一般個股／ETF 這一類單獨再看一次

    槓桿 ETF 與權證的筆數會隨市場波動起伏（重挫日整批跌停就多幾十筆），
    混在總數裡會把真正該注意的那一類稀釋掉。
    """

    ordinary: pd.DataFrame = split_by_category(unexplained)["一般個股ETF"]

    assert len(ordinary) <= BASELINE_UNEXPLAINED_ORDINARY + TOLERANCE, (
        f"一般個股／ETF 解釋不掉的跳空由 {BASELINE_UNEXPLAINED_ORDINARY} "
        f"增為 {len(ordinary)} 筆。最可疑的前五筆："
        f"{ordinary.nlargest(5, '停牌日數')[['date', 'stock_id', '推估倍率', '停牌日數']].to_dict('records')}"
    )
