"""
FinMind 入庫流程按資料集分檔

建表 DDL 與 SQL 一律在 `core/dao/tw/` 的 DAO；各子模組一律是**吃 `conn` 的模組層級函式**，
就地以該連線建 DAO，不自己持有連線：`FinMindLoader`
的 `connect()`／`disconnect()` 會換掉 `self.conn`，子模組若把連線存成自己的屬性，
斷線重連後就會拿著一個已關閉的連線。

對外的單一入口仍是 `core.pipeline.tw.loaders.finmind_loader.FinMindLoader`（門面）。
"""

__all__ = [
    "broker_info_loader",
    "broker_trading_loader",
    "reference_table_loader",
    "stock_info_loader",
]
