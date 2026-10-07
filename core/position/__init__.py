"""
部位管理層：部位進出、FIFO 拆單與帳務，回測與實盤共用

依市場分在子目錄（`base/`、`stock/`、`futures/`）。**刻意不做套件層 eager import**，
呼叫端一律指名模組（`from core.position.stock.position_manager import StockPositionManager`），
與 `core/portfolio/`、`core/datafeed/` 同一慣例。
"""
