from core.utils.constant.backtest import *  # noqa: F403
from core.utils.constant.cost import *  # noqa: F403
from core.utils.constant.futures import *  # noqa: F403
from core.utils.constant.io import *  # noqa: F403
from core.utils.constant.live import *  # noqa: F403
from core.utils.constant.market import *  # noqa: F403
from core.utils.constant.order import *  # noqa: F403

"""
交易相關常量：依領域分檔，套件層保留門面

常量依領域分檔（下單、成本、期貨商品、市場制度、回測政策、實盤鉤子、
檔案編碼），彼此互不相干；本檔只負責彙整。**檔數以上方的 import 區塊為準**，
這裡不複製數字——新增一檔時只會有人改 import，不會有人回來改這句話。

**門面用 star-import**：讓 `from core.utils.constant import Action` 這種寫法
（全庫上百處）維持可用。星號 import 的 `noqa` 是刻意的：這裡的用途就是轉出，
不是在自己的命名空間裡使用。
"""
