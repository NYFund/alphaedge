"""
具體策略：使用交易框架的程式，不屬於框架本身

- 本套件放**具體策略**（依商品分 `stock/`、`futures/`）；策略**契約**（`BaseStrategy` 與各商品基底）
  留在 `core/strategies/`——回測引擎、實盤引擎、報表都要認得那份介面，搬出去會讓 `core` 反向依賴本套件。
- 相依方向只能是 `strategies` → `core`：`core/` 內 import 本套件一律是反向相依，
  `scripts/check_layer_deps.py` 會擋下。
- **門面一律保持空白**：具體策略由策略載入器依名稱掃描載入，在 `__init__.py` eager import
  會讓每個 import 本套件的人都順帶執行所有策略模組。
"""
