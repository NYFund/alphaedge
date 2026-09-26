import argparse
import datetime
import sys
from typing import Any, List, Optional, Tuple

import pandas as pd

from core.config import TW_STOCK_DB_PATH
from core.dao.connection import DBConnection, DBError, connect_sqlite
from core.pipeline.tw.cleaners.corporate_action_detector import detect_unexplained_moves
from core.pipeline.tw.updaters.corporate_action_updater import CorporateActionUpdater

"""
公司行動的人工補登：列出偵測候選，確認後寫進 `corporate_action`

**為什麼需要人工這一關**：ETF 的受益權單位分割不在任何結構化端點裡
（`TWTB8U` 與 OpenAPI 的 143 個端點都查過），只能由偵測器從價格跳空產出候選。
而「這個跳空是分割還是資料錯誤」機器判不出來——分割的倍率乾淨（1 拆 4 ＝ 0.25），
資料錯誤的倍率也可能剛好接近某個整數比。判錯的代價是整段還原價都錯，
而還原價錯不會報錯，只會讓回測績效靜默偏掉。

**恢復買賣參考價必須由人提供**：它是官方公告的參考價，不等於恢復首日的收盤價
（0050 的 2025-06-18：參考價 47.1625、收盤 47.57）。可以給 `--reference-price`，
或給 `--ratio` 由「停止買賣前收盤價 × 倍率」推得——後者對 1 拆 N 更好核對。

**停止買賣前收盤價由本腳本自己從 `price` 表取**，不讓人手打：那個數字錯了倍率就錯，
而倍率是下游唯一會用到的東西。

用法（專案根目錄）：
    # 1. 只列候選，完全唯讀
    .venv/bin/python -m scripts.manual.manual_corporate_action_detect
    .venv/bin/python -m scripts.manual.manual_corporate_action_detect --start 2025-01-01

    # 2. 看過候選之後補登一筆（不帶 --confirm 只列計畫，不寫入）
    .venv/bin/python -m scripts.manual.manual_corporate_action_detect \\
        --date 2025-06-18 --symbol 0050 --reason "受益權單位分割 1拆4" --ratio 0.25
    .venv/bin/python -m scripts.manual.manual_corporate_action_detect \\
        --date 2025-06-18 --symbol 0050 --reason "受益權單位分割 1拆4" --ratio 0.25 --confirm

寫入是冪等的（主鍵 `(date, stock_id)` ＋ `INSERT OR REPLACE`），重跑同一筆不會長出第二列。
"""


def list_candidates(start_date: Optional[datetime.date], threshold: float) -> int:
    """列出偵測到的候選事件；完全唯讀"""

    candidates: pd.DataFrame = detect_unexplained_moves(
        threshold=threshold, start_date=start_date
    )

    if candidates.empty:
        print("沒有無法解釋的單日變動——事件表是齊的。")
        return 0

    print(f"偵測到 {len(candidates)} 筆無法由除權息或已知公司行動解釋的變動：\n")
    print(candidates.to_string(index=False))
    print(
        "\n逐筆判斷之後，以 `--date`／`--symbol`／`--reason` ＋ "
        "`--ratio` 或 `--reference-price` 補登。\n"
        "**倍率不乾淨的先當成資料錯誤查**，不要直接補登成公司行動。"
    )
    return 0


def get_previous_close(
    conn: DBConnection, symbol: str, date: datetime.date
) -> Tuple[Optional[str], Optional[float]]:
    """取該標的在 `date` **之前**最後一個交易日的（日期, 收盤價）"""

    row: Optional[Tuple[Any, ...]] = conn.execute(
        "SELECT date, 收盤價 FROM price WHERE stock_id = ? AND date < ? "
        "AND 收盤價 > 0 ORDER BY date DESC LIMIT 1",
        (symbol, date.isoformat()),
    ).fetchone()

    if row is None:
        return (None, None)
    return (str(row[0]), float(row[1]))


def get_security_name(conn: DBConnection, symbol: str) -> Optional[str]:
    """取該標的最近一筆的證券名稱"""

    row: Optional[Tuple[Any, ...]] = conn.execute(
        "SELECT 證券名稱 FROM price WHERE stock_id = ? ORDER BY date DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return None if row is None else str(row[0])


def apply_event(args: argparse.Namespace) -> int:
    """補登一筆人工確認過的公司行動"""

    date: datetime.date = datetime.date.fromisoformat(args.date)

    # 前收盤與名稱一律從庫裡取，唯讀連線
    conn: DBConnection = connect_sqlite(TW_STOCK_DB_PATH, read_only=True)
    try:
        previous_date, previous_close = get_previous_close(conn, args.symbol, date)
        name: Optional[str] = get_security_name(conn, args.symbol)
    finally:
        conn.close()

    if previous_close is None:
        print(f"❌ {args.symbol} 在 {date} 之前查不到收盤價，無法推出倍率")
        return 1
    if name is None:
        print(f"❌ {args.symbol} 查不到證券名稱")
        return 1

    if args.reference_price is not None:
        reference: float = args.reference_price
    else:
        reference = round(previous_close * args.ratio, 4)

    ratio: float = round(reference / previous_close, 6)

    print("補登計畫：")
    print(f"  標的         {args.symbol}（{name}）")
    print(f"  事件日       {date}")
    print(f"  停止買賣前   {previous_date} 收盤 {previous_close}")
    print(f"  恢復參考價   {reference}")
    print(f"  調整倍率     {ratio}")
    print(f"  原因         {args.reason}")

    if not args.confirm:
        print("\n未帶 `--confirm`，以上只是計畫，**沒有寫入任何東西**。")
        return 0

    updater: CorporateActionUpdater = CorporateActionUpdater()
    try:
        updater.setup()
        updater.load_detected_events(
            [
                {
                    "date": date.isoformat(),
                    "stock_id": args.symbol,
                    "證券名稱": name,
                    "停止買賣前收盤價": previous_close,
                    "恢復買賣參考價": reference,
                    "原因": args.reason,
                }
            ],
            file_name=f"detected_{args.symbol}_{date.isoformat()}.csv",
        )
    except (ValueError, OSError, DBError) as error:
        # 這條路徑真正會拋的三種：欄位缺漏（`ValueError`）、CSV 落地失敗（`OSError`）、
        # 資料庫錯誤（`DBError`）。**刻意不盲捕**：盲捕會把「程式寫錯」也變成一行
        # 「寫入失敗」，而手動補登出錯時最需要知道的就是它到底錯在哪一層。
        # 手動腳本的慣例是只印單行原因，不讓 traceback 帶出連線或路徑細節
        print(f"❌ 寫入失敗：{type(error).__name__}: {error}")
        return 1
    finally:
        updater.close()

    print("\n✅ 已寫入。重跑同一筆不會長出第二列（主鍵 ＋ `INSERT OR REPLACE`）。")
    print("接著跑一次假跳空護欄確認這筆真的解釋掉了那個跳空：")
    print("    .venv/bin/python -m pytest tests/test_corporate_action_guard.py -q")
    return 0


def main() -> int:
    """列候選（預設）或補登一筆"""

    parser = argparse.ArgumentParser(description="公司行動偵測與人工補登")
    parser.add_argument("--start", help="只看這一天之後（YYYY-MM-DD）")
    parser.add_argument(
        "--threshold", type=float, default=0.15, help="單日變動門檻（小數，預設 0.15）"
    )
    parser.add_argument("--date", help="要補登的事件日（YYYY-MM-DD）")
    parser.add_argument("--symbol", help="要補登的標的代號")
    parser.add_argument("--reason", help="原因（會原樣寫進表）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--ratio", type=float, help="調整倍率（1 拆 4 ＝ 0.25）")
    group.add_argument("--reference-price", type=float, help="官方恢復買賣參考價")
    parser.add_argument("--confirm", action="store_true", help="真的寫入")
    args = parser.parse_args()

    supplied: List[str] = [
        name for name in ("date", "symbol", "reason") if getattr(args, name)
    ]
    if not supplied:
        start: Optional[datetime.date] = (
            datetime.date.fromisoformat(args.start) if args.start else None
        )
        return list_candidates(start, args.threshold)

    missing: List[str] = [
        name for name in ("date", "symbol", "reason") if not getattr(args, name)
    ]
    if missing:
        print(f"❌ 補登需要同時給 {missing}")
        return 2
    if args.ratio is None and args.reference_price is None:
        print("❌ 補登需要 `--ratio` 或 `--reference-price` 其中之一")
        return 2

    return apply_event(args)


if __name__ == "__main__":
    sys.exit(main())
