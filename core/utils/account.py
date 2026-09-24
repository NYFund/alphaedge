"""
Shioaji 金鑰容器

僅供 tick 爬蟲的多帳號輪替使用（`StockTickUtils.setup_shioaji_apis()`）。
登入、登出、帳務與損益查詢**不走這裡**，一律用 `core/broker/tw/` 的
`ShioajiSession` 與 `ShioajiAccountQuery`。
"""


class ShioajiAPI:
    """Shioaji API_KEY and API_SECRET_KEY"""

    def __init__(self, api_key: str, api_secret_key: str) -> None:
        self.api_key: str = api_key
        self.api_secret_key: str = api_secret_key
