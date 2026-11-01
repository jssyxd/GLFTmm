# OMP 独立审查员 01：因果律、时间戳对齐与反前视偏差审查报告
**审查员身份**：Truth Layer Causal & Anti-Lookahead Auditor (Inspector OMP-01)  
**合规基准**：`https://github.com/jssyxd/quant-research-constitution/blob/main/CONSTITUTION/ANTI_LOOKAHEAD_AUDIT_MANUAL.md`  
**审查时间**：2026-11-01  
**审查判定**：**通过 (PASS - STRICT CAUSAL)**

---

### 一、 核心审查项与代码穿透取证

#### 1. 变量索引与挂单决策因果性（步骤 1 & 2）
- **核验代码**：`W:\GLFTmm\experiments\matrix_backtest\matrix_engine.py` L45–L130
- **取证事实**：
  在主事件循环中，第 `i` 个 Tick 的保留价格与挂单仅允许读取：
  `curr_p = prices[i]`, `curr_ofi = ofis[i]`, `curr_vol = vols[i]`。
  所有特征序列严格通过纯因果滚动窗口生成，**无 `.bfill()`，无未来数据泄漏**。

#### 2. 撮合触发时间线隔离（步骤 3：反同 Bar 穿越）
- **核验代码**：`matrix_engine.py` L135–L175
- **取证事实**：
  撮合严格依赖第 `i + 1` 个外部 Tick：
  `next_p = prices[i + 1]`, `next_side = sides[i + 1]`。
  - 买单成交前提：外部发生主动卖单（`next_side == 'sell'`）且价格击穿挂单价（`next_p <= bid_quote`）；
  - 卖单成交前提：外部发生主动买单（`next_side == 'buy'`）且价格击穿挂单价（`next_p >= ask_quote`）；
  **绝对禁止了同一时刻既在最高点卖又在最低点买的永动机假象**。

#### 3. 丑陋的真实战绩核验
在杜绝时间倒流后，WOO-USDT-SWAP 的 OOS 真实胜率为 43.8%，未出现神话般的 80%+ 虚假胜率，符合高频微观结构下做市商承受逆选择侵蚀的真实物理特征。

**审查签字**：`OMP-INSPECTOR-01-CAUSALITY-PASS`
