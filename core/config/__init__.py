"""
設定門面：維持 `from core.config import X` 這個既有的 import 路徑不變

設定依「什麼時候會改」分成三個模組，本檔只負責彙整：

| 模組 | 內容 | 什麼時候會改 |
|------|------|--------------|
| `paths` | 原始碼路徑、產物三根與其下所有目錄 | 目錄搬遷時 |
| `schema` | 分庫檔名、完整路徑、資料表名稱 | 新增資料表時 |
| `settings` | 爬取範圍、預設區間、DolphinDB／Shioaji 憑證 | 調整營運參數時 |

**新程式碼建議直接 import 子模組**（`from core.config.paths import DATA_DIR_PATH`），
語意較明確；本門面是為了讓既有呼叫端的 `from core.config import X` 繼續可用。

**為什麼用 star import 而不逐一列出**：逐一列出會埋一個陷阱——日後在 `paths.py`
新增一個常數卻忘了補進這裡，`from core.config import NEW_PATH` 會以
ImportError 收場，而錯誤訊息完全指不到真正的原因。
"""

from .paths import *  # noqa: F403
from .schema import *  # noqa: F403
from .settings import *  # noqa: F403
