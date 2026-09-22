"""
Shioaji 金鑰容器

只剩 tick 爬蟲的多帳號輪替在用（`StockTickUtils.setup_shioaji_apis()`）。登入、登出、
帳務與損益查詢已由 `core/broker/tw/` 的 `ShioajiSession`、`ShioajiAccountQuery` 取代。
"""


class ShioajiAPI:
    """Shioaji API_KEY and API_SECRET_KEY"""

    def __init__(self, api_key: str, api_secret_key: str) -> None:
        self.api_key: str = api_key
        self.api_secret_key: str = api_secret_key
