from enum import Enum

"""
檔案 I/O 常量：CSV 編碼

**鏡像常數與它的 Enum 放在同一檔**：分開放會讓「改 Enum 要記得改鏡像」跨兩個檔案，而那正是兩者最容易漂移的時候。
"""


class FileEncoding(str, Enum):
    """檔案編碼類型"""

    UTF8 = "utf-8"
    UTF8_SIG = "utf-8-sig"  # UTF-8 with BOM，用於 Excel 等軟體正確識別中文
    BIG5 = "big5"
