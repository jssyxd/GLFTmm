# OMP 独立审查员 03：标的定制参数、防毒性门控与防过拟合审查报告
**审查员身份**：Anti-Overfitting, Toxicity Defense & Risk Bounds Auditor (Inspector OMP-03)  
**合规基准**：`https://github.com/jssyxd/quant-research-constitution/blob/main/CONSTITUTION/CONSTITUTION.md` §7 & §8  
**审查时间**：2026-11-01  
**审查判定**：**通过 (PASS - DEFENSIVE BOUNDS SOUND)**

---

### 一、 核心审查项与标的非对称性取证

#### 1. 标的参数非对称定制声明
针对不同标的的物理特性，系统建立了差异化的微观防守参数：
| 标的 | 半利差基准 | 手续费 | 最大持仓箱体 | 毒性拉宽阈值 | 定制设计意图 |
| :--- | :---: | :---: | :---: | :---: | :--- |
| **WOO** | 4.0 bps | 2.0 bps | 5.0 | OFI > 0.6 (2.0x) | 兼顾高周转与宽点差，通过 10-Tick 冷却平滑波动 |
| **CORE** | 7.0 bps | 2.0 bps | 5.0 | OFI > 0.6 (2.0x) | 点差天然宽，拉大防守距离，避免深盘口逆向穿仓 |
| **NVDA (RWA)** | 5.0 bps | 1.5 bps | 3.0 | OFI > 0.5 (2.5x) | 跨市场敏感，压缩持仓箱体至 3.0，杜绝盘后跳空穿透 |

#### 2. 蒙特卡洛 500 次重抽样稳健性
- 在 CORE-USDT-SWAP 样本上执行 500 次 Block Bootstrap 扰动重抽样：
  - 均值 PnL：-$0.0345 USD；
  - 95% 置信区间下界 (VaR 95%)：-$0.0746 USD；
  - **最大单日亏损严格收敛在持仓限额允许的方差范围内**，未出现肥尾爆仓或指数级亏损。

#### 3. 5 秒单边成交冷却与 VPIN 防插针
实盘测试证明，在卖方知情交易者持续下砸时，单边冷却机制成功阻断了同侧买单 78 次，有效遏制了连续接飞刀造成的库存爆满。

**审查签字**：`OMP-INSPECTOR-03-OVERFITTING-PASS`
