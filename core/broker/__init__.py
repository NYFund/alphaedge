from .base import BaseBroker

"""
券商閘道層：把「委託送出去、回報收回來、帳務查回來」收斂成單一介面

**只可 import `core.config`／`core.utils`／`core.models`**，
特別是**不可 import `core.api`**——券商層不讀歷史資料。它一旦開始查價格庫，
實盤與回測就會各有一套取數路徑，而兩套路徑一定會在某天給出不同的數字。
"""

__all__ = ["BaseBroker"]
