"""
資料存取層（DAO）：SQL、連線與交易只寫在這一層

`base.py` 與 `connection.py` 是市場無關的底座；各市場的資料表 DAO 在子目錄（`tw/`），
目錄只承載「市場」一條軸（`scripts/check_layer_deps.py` 會擋跨軸混放）。

本套件位於 `core.config` 之上、`core.api`／`core.pipeline` 之下，**只可 import
`core.config`**：DAO 只負責 SQL、連線與交易，業務規則（單位換算、交易時段語意等）
屬於上層，不該滲進這一層。這是慣例而非機器護欄——`scripts/check_layer_deps.py`
只會把 import `core.api`／`core.pipeline` 判成反向相依；`core.utils` 層級更低、
`core.models` 同層，import 它們都不會被擋。
"""
