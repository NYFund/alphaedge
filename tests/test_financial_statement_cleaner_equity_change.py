from io import StringIO
from pathlib import Path
from typing import List

import pandas as pd
import pytest

from core.pipeline.tw.cleaners.financial_statement_cleaner import (
    FinancialStatementCleaner,
)

"""
權益變動表清洗測試

驗證二維表（欄＝權益項目、列＝變動原因）攤平成長表的規則，
不連網路、不連 DB；CSV 輸出導向 tmp_path，不污染 downloads 目錄。

測試資料照抄 MOPS `ajax_t164sb06` 的實際版面（2330 台積電 2024Q1 節錄）：
- 表頭有兩層，第一層是「民國113年第1季」，第二層全是「單位：新台幣仟元」
- 真正的欄位名稱在**內容第一列**（會計項目、普通股股本、…）
- 同一頁還附一張去年同季（民國112年第1季）的比較表，兩張版面完全相同
- 庫藏股票欄為空白（台積電無庫藏股）
"""

# 兩張表的欄位刻意不同（比較表少了「庫藏股票」），
# 抓錯表時除了數字不同，欄位數也會露餡
CURRENT_PERIOD_HTML: str = """
<table>
  <thead>
    <tr><th colspan="5">民國113年第1季</th></tr>
    <tr><th>單位：新台幣仟元</th><th>單位：新台幣仟元</th><th>單位：新台幣仟元</th>
        <th>單位：新台幣仟元</th><th>單位：新台幣仟元</th></tr>
  </thead>
  <tbody>
    <tr><td>會計項目</td><td>普通股股本</td><td>資本公積</td><td>庫藏股票</td><td></td></tr>
    <tr><td>期初餘額</td><td>259320710</td><td>69876381</td><td></td><td></td></tr>
    <tr><td>普通股現金股利</td><td>0</td><td>0</td><td></td><td></td></tr>
    <tr><td>權益增加（減少）總額</td><td>16905</td><td>546053</td><td></td><td></td></tr>
    <tr><td>期末餘額</td><td>259337615</td><td>70422434</td><td></td><td></td></tr>
  </tbody>
</table>
"""

PRIOR_PERIOD_HTML: str = """
<table>
  <thead>
    <tr><th colspan="4">民國112年第1季</th></tr>
    <tr><th>單位：新台幣仟元</th><th>單位：新台幣仟元</th>
        <th>單位：新台幣仟元</th><th>單位：新台幣仟元</th></tr>
  </thead>
  <tbody>
    <tr><td>會計項目</td><td>普通股股本</td><td>資本公積</td><td></td></tr>
    <tr><td>期初餘額</td><td>259303805</td><td>69330328</td><td></td></tr>
    <tr><td>期末餘額</td><td>259320710</td><td>69876381</td><td></td></tr>
  </tbody>
</table>
"""

# 頁面最上方的公告文字也是一張 table，read_html 會一併讀進來
NOTICE_HTML: str = """
<table>
  <tbody>
    <tr><td>「投資人若需了解更詳細資訊可至XBRL資訊平台或電子書查詢」</td></tr>
    <tr><td>本公司採 月制會計年度(空白表曆年制)</td></tr>
  </tbody>
</table>
"""


def build_df_list() -> List[pd.DataFrame]:
    """組出 MOPS 單一公司頁面回傳的表格清單（公告表 ＋ 本期表 ＋ 去年同季比較表）"""

    return (
        pd.read_html(StringIO(NOTICE_HTML))
        + pd.read_html(StringIO(CURRENT_PERIOD_HTML))
        + pd.read_html(StringIO(PRIOR_PERIOD_HTML))
    )


@pytest.fixture
def cleaner(tmp_path: Path) -> FinancialStatementCleaner:
    """權益變動表清洗器，輸出目錄改指 tmp_path"""

    fs_cleaner: FinancialStatementCleaner = FinancialStatementCleaner()
    fs_cleaner.equity_change_dir = tmp_path
    return fs_cleaner


def test_clean_equity_changes_flattens_to_long_format(
    cleaner: FinancialStatementCleaner,
) -> None:
    """二維表攤平成長表：一列 = 一個（權益項目 × 變動原因）的金額"""

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    assert list(df.columns) == cleaner.EQUITY_CHANGE_COLS
    # 兩個權益項目 × 四個變動原因；庫藏股票整欄空白，不佔列
    assert len(df) == 8
    assert set(df["權益項目"]) == {"普通股股本", "資本公積"}
    assert (df["year"] == 2024).all()
    assert (df["season"] == 1).all()
    assert (df["stock_id"] == "2330").all()


def test_clean_equity_changes_keeps_amounts_numeric(
    cleaner: FinancialStatementCleaner,
) -> None:
    """金額轉為數值型別，且對得回原始儲存格"""

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    assert pd.api.types.is_numeric_dtype(df["金額"])

    opening: pd.DataFrame = df[
        (df["權益項目"] == "普通股股本") & (df["變動原因"] == "期初餘額")
    ]
    assert opening["金額"].iloc[0] == 259320710


def test_clean_equity_changes_picks_current_period_only(
    cleaner: FinancialStatementCleaner,
) -> None:
    """
    只取本期表，不取去年同季的比較表

    兩張表的版面一模一樣，抓錯會把去年的數字記成今年的，
    而且因為主鍵相同，錯誤資料還會安靜地佔住正確資料的位置
    """

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    ending: pd.DataFrame = df[
        (df["權益項目"] == "普通股股本") & (df["變動原因"] == "期末餘額")
    ]
    # 本期期末 259337615；若誤抓比較表會是 259320710
    assert ending["金額"].iloc[0] == 259337615


def test_clean_equity_changes_drops_blank_cells(
    cleaner: FinancialStatementCleaner,
) -> None:
    """空白儲存格（台積電沒有庫藏股票）不入長表，不存一整排 NULL"""

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    assert "庫藏股票" not in set(df["權益項目"])
    assert df["金額"].notna().all()


def test_clean_equity_changes_standardizes_full_width_parentheses(
    cleaner: FinancialStatementCleaner,
) -> None:
    """
    變動原因的全形括號一律正規化成半形

    來源網站同一個項目跨年度會在全形／半形之間擺盪（2013 年是
    「權益增加（減少）總額」、2024 年是「權益增加(減少)總額」），
    不統一就會在同一張表裡變成兩個不同的項目
    """

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    assert "權益增加(減少)總額" in set(df["變動原因"])
    assert "權益增加（減少）總額" not in set(df["變動原因"])


def test_clean_equity_changes_returns_empty_when_period_missing(
    cleaner: FinancialStatementCleaner,
) -> None:
    """要求的年季不在頁面上時回傳空表，而不是退而求其次抓別季的表"""

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2025, season=3, stock_id="2330"
    )

    assert df.empty
    assert list(df.columns) == cleaner.EQUITY_CHANGE_COLS


def test_save_equity_changes_merges_batch_into_one_csv(
    cleaner: FinancialStatementCleaner, tmp_path: Path
) -> None:
    """一批多檔股票合併成單一 CSV：逐檔一個檔案會產生十萬個小檔"""

    df_2330: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )
    df_2454: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2454"
    )

    file_path: Path = cleaner.save_equity_changes(
        df_list=[df_2330, df_2454], year=2024, season=1
    )

    assert file_path == tmp_path / "equity_change_2024Q1_0000.csv"
    saved_df: pd.DataFrame = pd.read_csv(file_path, dtype={"stock_id": str})
    assert len(saved_df) == len(df_2330) + len(df_2454)
    assert set(saved_df["stock_id"]) == {"2330", "2454"}


def test_save_equity_changes_skips_empty_batch(
    cleaner: FinancialStatementCleaner, tmp_path: Path
) -> None:
    """整批都沒資料時不落地空檔案，免得 loader 得為空檔跑一次入庫"""

    file_path = cleaner.save_equity_changes(
        df_list=[pd.DataFrame(columns=cleaner.EQUITY_CHANGE_COLS)],
        year=2024,
        season=1,
    )

    assert file_path is None
    assert not list(tmp_path.glob("*.csv"))


def test_save_equity_changes_does_not_overwrite_previous_run(
    cleaner: FinancialStatementCleaner, tmp_path: Path
) -> None:
    """
    同一年季跑第二次時，批次序號要接續而非從 0 重數

    重跑同一年季是常態（resume 續跑、補暫時性失敗、新上市公司補舊季），
    而每次執行涵蓋的股票子集不同、同名檔的內容也就不同。
    2026-08-22 的 2020Q1 補跑實際踩過：首輪寫了 16 批，補跑從 0000 重數，
    磁碟上少了 300 檔的紀錄。
    """

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )

    # 第一次執行：兩批
    first: Path = cleaner.save_equity_changes([df], year=2024, season=1)
    second: Path = cleaner.save_equity_changes([df], year=2024, season=1)
    assert [first.name, second.name] == [
        "equity_change_2024Q1_0000.csv",
        "equity_change_2024Q1_0001.csv",
    ]

    # 第二次執行（另一個 cleaner，模擬新的行程）：序號要從 0002 接下去
    rerun_cleaner: FinancialStatementCleaner = FinancialStatementCleaner()
    rerun_cleaner.equity_change_dir = tmp_path
    third: Path = rerun_cleaner.save_equity_changes([df], year=2024, season=1)

    assert third.name == "equity_change_2024Q1_0002.csv"
    assert first.exists() and second.exists()
    assert len(list(tmp_path.glob("equity_change_2024Q1_*.csv"))) == 3


def test_next_equity_changes_batch_index_is_per_season(
    cleaner: FinancialStatementCleaner, tmp_path: Path
) -> None:
    """序號各年季獨立計算，別季的檔案不影響本季"""

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=build_df_list(), year=2024, season=1, stock_id="2330"
    )
    cleaner.save_equity_changes([df], year=2024, season=1)

    assert cleaner.next_equity_changes_batch_index(2024, 1) == 1
    assert cleaner.next_equity_changes_batch_index(2024, 2) == 0
    assert cleaner.next_equity_changes_batch_index(2023, 1) == 0


def test_next_equity_changes_batch_index_ignores_renamed_files(
    cleaner: FinancialStatementCleaner, tmp_path: Path
) -> None:
    """人工改過名的檔案只警告、不讓整條流程停擺"""

    (tmp_path / "equity_change_2024Q1_backup.csv").write_text("x", encoding="utf-8")

    assert cleaner.next_equity_changes_batch_index(2024, 1) == 0


# === 期別標籤：只有 Q1 是「第N季」 ===
def build_seasonal_html(period_label: str, opening: int) -> str:
    """組出指定期別標籤的權益變動表 HTML（版面與本期表相同，只有標籤與金額不同）"""

    return f"""
    <table>
      <thead>
        <tr><th colspan="3">{period_label}</th></tr>
        <tr><th>單位：新台幣仟元</th><th>單位：新台幣仟元</th>
            <th>單位：新台幣仟元</th></tr>
      </thead>
      <tbody>
        <tr><td>會計項目</td><td>普通股股本</td><td></td></tr>
        <tr><td>期初餘額</td><td>{opening}</td><td></td></tr>
      </tbody>
    </table>
    """


@pytest.mark.parametrize(
    "season, period_label",
    [
        (1, "民國109年第1季"),
        (2, "民國109年上半年度"),
        (3, "民國109年前3季"),
        (4, "民國109年度"),
    ],
)
def test_clean_equity_changes_matches_every_season_label(
    cleaner: FinancialStatementCleaner, season: int, period_label: str
) -> None:
    """
    四季的期別標籤都要對得上

    原本寫死成 `f"民國{roc_year}年第{season}季"`，只有 Q1 對得上——
    2026-09-03 的 2020Q2 全市場回補因此打了 2,087 次請求、跑 1.5 小時、
    入庫 0 列，而統計行三個數字全都正常、結束碼 0。
    照那個版本跑完整段回補，54 個年季裡只有 14 個 Q1 會有資料。
    """

    df_list: List[pd.DataFrame] = pd.read_html(
        StringIO(build_seasonal_html(period_label, 259303805))
    )

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=df_list, year=2020, season=season, stock_id="2330"
    )

    assert not df.empty
    assert df["金額"].iloc[0] == 259303805


@pytest.mark.parametrize(
    "season, current_label, prior_label",
    [
        (2, "民國109年上半年度", "民國108年上半年度"),
        (3, "民國109年前3季", "民國108年前3季"),
        (4, "民國109年度", "民國108年度"),
    ],
)
def test_clean_equity_changes_picks_current_period_in_every_season(
    cleaner: FinancialStatementCleaner,
    season: int,
    current_label: str,
    prior_label: str,
) -> None:
    """
    非 Q1 的頁面同樣附去年同期表，一樣不能抓錯

    去年同期的標籤是同一組措辭、只有民國年不同，故補齊標籤的同時
    仍要靠「民國{roc_year}年」前綴區分本期與去年
    """

    df_list: List[pd.DataFrame] = pd.read_html(
        StringIO(build_seasonal_html(current_label, 259303805))
    ) + pd.read_html(StringIO(build_seasonal_html(prior_label, 111111111)))

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=df_list, year=2020, season=season, stock_id="2330"
    )

    assert df["金額"].iloc[0] == 259303805


def test_clean_equity_changes_still_rejects_wrong_year(
    cleaner: FinancialStatementCleaner,
) -> None:
    """補齊標籤不得放寬年份：只有去年同期表的頁面必須回空表，不能將就"""

    df_list: List[pd.DataFrame] = pd.read_html(
        StringIO(build_seasonal_html("民國108年上半年度", 111111111))
    )

    df: pd.DataFrame = cleaner.clean_equity_changes(
        df_list=df_list, year=2020, season=2, stock_id="2330"
    )

    assert df.empty
