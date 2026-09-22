import itertools
from typing import Dict, List

import pandas as pd

from core.backtest.models.cost_model import StockCostModel
from core.models.cost_config import CostConfig
from core.utils.constant import Action, PositionType
from core.utils.instrument import StockUtils

"""
LONG 成本口徑差異比對（分析工具，非測試；保留供日後調整成本模型時重跑）

用途：在動生產程式碼**之前**量化「舊 `StockUtils` 路徑」與「新 `StockCostModel` 路徑」
的差異分布，避免拿回歸測試的紅燈當探索工具。

兩條路徑的實際差異只有一處——**部分平倉時開倉手續費的攤提方式**：

| | 舊路徑（`position_manager` LONG 分支） | 新路徑（`StockCostModel`） |
|---|---|---|
| 記錄用的開倉手續費 | `int(原始手續費 × 平倉張數 / 開倉張數)`（等比例攤提） | 同左 |
| **損益用的開倉手續費** | `calculate_net_profit()` 內部**以平倉張數重算**，最低手續費 20 元會**再套一次** | 沿用等比例攤提值 |

也就是說舊路徑的 `record.commission` 與 `record.realized_pnl` 對開倉手續費用了
**兩個不同的數字**，全額平倉時兩者相等所以看不出來，部分平倉才會分岔。

## 實測結果（2026-08-15，2,448 組；其中部分平倉 1,440 組）

| 項目 | 有差異筆數 | 占比 | 最大差 | **全額平倉的差異** |
|------|-----------|------|--------|------------------|
| `commission` | 0 | 0.0% | 0 | 0 |
| `tax` | 0 | 0.0% | 0 | 0 |
| `realized_pnl` | 288 | 11.8% | 18.00 元 | **0** |
| `roi` | 268 | 10.9% | 142.88%（分母極小者） | **0** |

差異 **100% 落在部分平倉**，其中 **91.7%** 觸及最低手續費 20 元門檻。

據此於 2026-08-15 將 `position_manager.py` 的 LONG 分支收斂到 `StockCostModel`。
LONG baseline 的 915 筆全部是全額平倉，故該次切換**未改變 baseline**，回歸雙線逐筆相同。

執行：`python -m tests.backtest.compare_cost_formula`
"""

# 涵蓋各檔位級距的邊界價（台股升降單位在 10／50／100／500／1000 換檔）
PRICES: List[float] = [
    5.0,
    9.99,
    10.0,
    49.95,
    50.0,
    99.5,
    100.0,
    499.0,
    500.0,
    999.0,
    1000.0,
    2000.0,
]

# 開倉張數；含 1 張（最低手續費必然觸發）與大額（最低手續費不觸發）
OPEN_VOLUMES: List[int] = [1, 2, 3, 5, 10, 50, 100]

# 最低手續費（差異幾乎全部集中在這個門檻上）
MIN_FEE: int = 20


def build_cost_model() -> StockCostModel:
    """建立與生產路徑同設定的成本模型"""

    return StockCostModel(CostConfig())


def compute_old(
    buy_price: float,
    sell_price: float,
    open_volume: int,
    close_volume: int,
) -> Dict[str, float]:
    """舊路徑：`position_manager.close_long_position()` 的實際算法"""

    open_commission: int = StockUtils.calculate_transaction_commission(
        price=buy_price, volume=open_volume
    )
    proportional_buy_commission: int = int(
        open_commission * (close_volume / open_volume)
    )
    sell_commission: int = StockUtils.calculate_transaction_commission(
        price=sell_price, volume=close_volume
    )
    sell_tax: int = StockUtils.calculate_transaction_tax(sell_price, close_volume)

    return {
        "commission": proportional_buy_commission + sell_commission,
        "tax": sell_tax,
        # 注意：損益走 calculate_net_profit，其開倉手續費是以平倉張數「重算」的
        "realized_pnl": StockUtils.calculate_net_profit(
            buy_price=buy_price, sell_price=sell_price, volume=close_volume
        ),
        "roi": StockUtils.calculate_roi(
            buy_price=buy_price, sell_price=sell_price, volume=close_volume
        ),
    }


def compute_new(
    cost_model: StockCostModel,
    buy_price: float,
    sell_price: float,
    open_volume: int,
    close_volume: int,
) -> Dict[str, float]:
    """新路徑：`StockCostModel` ＋ 等比例攤提"""

    open_commission: int = cost_model.commission(price=buy_price, volume=open_volume)
    entry_cost: int = int(open_commission * (close_volume / open_volume))
    exit_commission: int = cost_model.commission(price=sell_price, volume=close_volume)
    exit_tax: int = cost_model.tax(
        price=sell_price, volume=close_volume, action=Action.SELL, is_day_trade=False
    )
    exit_cost: int = exit_commission + exit_tax

    return {
        "commission": entry_cost + exit_commission,
        "tax": exit_tax,
        "realized_pnl": cost_model.realized_pnl(
            position_type=PositionType.LONG,
            entry_price=buy_price,
            exit_price=sell_price,
            volume=close_volume,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
        ),
        "roi": cost_model.roi(
            position_type=PositionType.LONG,
            entry_price=buy_price,
            exit_price=sell_price,
            volume=close_volume,
            entry_cost=entry_cost,
            exit_cost=exit_cost,
        ),
    }


def sweep() -> pd.DataFrame:
    """掃過所有組合（含部分平倉），輸出兩條路徑的四項數值與差值"""

    cost_model: StockCostModel = build_cost_model()
    rows: List[Dict[str, float]] = []

    for buy_price, sell_price, open_volume in itertools.product(
        PRICES, PRICES, OPEN_VOLUMES
    ):
        for close_volume in sorted({1, open_volume // 2, open_volume} - {0}):
            old: Dict[str, float] = compute_old(
                buy_price, sell_price, open_volume, close_volume
            )
            new: Dict[str, float] = compute_new(
                cost_model, buy_price, sell_price, open_volume, close_volume
            )
            row: Dict[str, float] = {
                "buy_price": buy_price,
                "sell_price": sell_price,
                "open_volume": open_volume,
                "close_volume": close_volume,
                "is_partial": close_volume < open_volume,
            }
            for key in ("commission", "tax", "realized_pnl", "roi"):
                row[f"old_{key}"] = old[key]
                row[f"new_{key}"] = new[key]
                row[f"diff_{key}"] = round(new[key] - old[key], 6)
            rows.append(row)

    return pd.DataFrame(rows)


def report(df: pd.DataFrame) -> None:
    """輸出差異分布報告"""

    print(f"組合總數：{len(df):,}（其中部分平倉 {int(df['is_partial'].sum()):,}）")
    print()
    print(
        f"{'項目':<14}{'有差異筆數':>12}{'占比':>9}{'最大差':>12}{'全額平倉差異':>14}"
    )
    print("-" * 62)

    for key in ("commission", "tax", "realized_pnl", "roi"):
        col: pd.Series = df[f"diff_{key}"]
        differing: pd.Series = col != 0
        full_close_diff: int = int((differing & ~df["is_partial"]).sum())
        print(
            f"{key:<14}{int(differing.sum()):>12,}{differing.mean() * 100:>8.1f}%"
            f"{col.abs().max():>12.2f}{full_close_diff:>14,}"
        )

    print()
    partial: pd.DataFrame = df[df["is_partial"]]
    diff_rows: pd.DataFrame = partial[partial["diff_realized_pnl"] != 0]
    print(f"差異全部落在部分平倉：{len(diff_rows):,} / {len(partial):,} 筆部分平倉組合")

    if not diff_rows.empty:
        # 舊路徑重算的開倉手續費是否等於最低手續費 —— 驗證「差異集中在 20 元門檻」
        recomputed: pd.Series = diff_rows.apply(
            lambda r: StockUtils.calculate_transaction_commission(
                price=r["buy_price"], volume=int(r["close_volume"])
            ),
            axis=1,
        )
        at_min_fee: float = (recomputed == MIN_FEE).mean() * 100
        print(f"其中舊路徑重算後觸及最低手續費（{MIN_FEE} 元）者：{at_min_fee:.1f}%")
        print()
        print("差異最大的五筆：")
        top: pd.DataFrame = diff_rows.reindex(
            diff_rows["diff_realized_pnl"].abs().sort_values(ascending=False).index
        ).head(5)
        print(
            top[
                [
                    "buy_price",
                    "sell_price",
                    "open_volume",
                    "close_volume",
                    "old_realized_pnl",
                    "new_realized_pnl",
                    "diff_realized_pnl",
                ]
            ].to_string(index=False)
        )


def main() -> None:
    df: pd.DataFrame = sweep()
    report(df)

    output_path: str = "tests/backtest/cost_formula_diff.csv"
    df.to_csv(output_path, index=False)
    print(f"\n完整比對結果已輸出：{output_path}")


if __name__ == "__main__":
    main()
