# strategies 目錄規範（Claude Code）

> 只在 `strategies/` 底下讀寫檔案時載入（具體策略都在這裡；`core/strategies/` 只放契約）。通用 coding style 見根目錄 `CLAUDE.md` §2。

## 策略類別 docstring

策略類別的 class docstring 用條列描述交易邏輯，區塊固定為「買進條件 / 賣出條件 / 停損條件」，並註明未實作的部分：

```python
class VolumeBreakoutMomentumStrategy(BaseStockStrategy):
    """
    量能突破動能策略（日線）

    買進條件（全部滿足）：
    - 當日收盤相對「前一交易日」收盤漲幅 ≥ 門檻（預設 9%）

    停損條件：
    - 未實作（一律不回傳停損單）
    """
```
