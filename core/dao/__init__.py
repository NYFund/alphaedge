"""
資料存取層（DAO）：SQL、連線與交易只寫在這一層

`base.py` 與 `connection.py` 是市場無關的底座；各市場的資料表 DAO 在子目錄（`tw/`），
目錄只承載「市場」一條軸（`scripts/check_layer_deps.py` 會擋跨軸混放）。

本套件位於 `core.config` 之上、`core.api`／`core.pipeline` 之下，**只可 import
`core.config`**：`core.utils` 內有模組會反向拉進回測層，DAO 一旦相依它就會把
整條回測引擎帶進 ETL。
"""
