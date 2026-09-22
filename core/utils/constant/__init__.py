from core.utils.constant.backtest import *  # noqa: F403
from core.utils.constant.cost import *  # noqa: F403
from core.utils.constant.futures import *  # noqa: F403
from core.utils.constant.io import *  # noqa: F403
from core.utils.constant.live import *  # noqa: F403
from core.utils.constant.market import *  # noqa: F403
from core.utils.constant.order import *  # noqa: F403

"""
交易相關常量：依領域分檔，套件層保留門面

原本是單一檔案（679 行、32 個 Enum ＋ 79 個鏡像常數），橫跨下單、成本、期貨商品、
市場制度、回測政策、實盤鉤子六個互不相干的領域——與 `core/config/` 拆分前同一個形狀。

**門面用 star-import**：`from core.utils.constant import Action` 這種既有寫法
（全庫上百處）一行都不必改。星號 import 的 `noqa` 是刻意的：這裡的用途就是轉出，
不是在自己的命名空間裡使用。
"""
