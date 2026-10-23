from typing import NamedTuple, Optional

"""PriceLimits: 具名的漲跌停結果（回測規格、實盤資料源、執行層三處共用）"""


class PriceLimits(NamedTuple):
    """
    當日漲跌停：`limit_up` 漲停、`limit_down` 跌停；本市場沒有這個制度或取不到時為 None

    **一定要用欄位名存取，不要解包成 tuple**：三個同名的 `get_price_limits()` 原本回的 tuple
    順序不同（規格層回（跌停, 漲停）、實盤回（漲停, 跌停）），只要有一處寫反就是靜默錯價，
    而且不會有任何錯誤訊息。欄位順序與 vn.py 的 `TickData.limit_up`／`limit_down` 一致。
    """

    limit_up: Optional[float]
    limit_down: Optional[float]

    @classmethod
    def none(cls) -> "PriceLimits":
        """兩側都取不到（或本市場沒有漲跌停制度）"""

        return cls(limit_up=None, limit_down=None)
