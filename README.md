# GLFTmm: 跨领域高频量化做市与主动防御系统 (全分支工程落地架构)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![Engine: NautilusTrader & High-Precision Event-Driven](https://img.shields.io/badge/Engine-NautilusTrader-orange.svg)]()

> 本项目由原 `asgrid-arb` 演化重构而来，彻底淘汰古典 Avellaneda-Stoikov (A-S) 的 $(T-t)$ 震荡缺陷，全面引入 **Stationary GLFT（无限视界稳态偏微分方程解）+ 手续费内生化硬门槛 + LightGBM 毒性门控 + VPIN 动态熔断 + 5秒单边成交冷却** 机制，支撑三大跨领域核心业务分支。

---

## 一、 系统三大业务分支与目录结构

本项目严格按照三大核心做市与套利方向划分为独立业务分支，共享底层稳态定价数学核与毒性防御网络：

```
GLFTmm/
├── 01_crypto_altcoin_perp_mm/       # 话题 1：加密山寨永续合约 MM (高换手、防砸盘、防插针)
│   ├── core/                        # GLFT 稳态报价、5s 冷却、库存非线性硬箱体
│   ├── models/                      # VPIN 熔断计算器、LightGBM 逆向选择前置毒性感知
│   └── backtest/                    # OKX/Hyperliquid 高保真 L2/Trades 离线撮合与回测
├── 02_rwa_tokenized_equity_mm/      # 话题 2：RWA 链上代币化正股做市 (HIP-3 / xStocks 连续交易 vs 传统闭盘套利)
│   ├── core/                        # 均值回归基差因子注入、周末自治订单簿报价
│   ├── regime/                      # 美股/韩股开闭盘状态机 (Regime 0/1/2 分级防御)
│   └── backtest/                    # 跨市场跳空防插针 (Gap Defense) 历史事件驱动回测
├── 03_dex_concentrated_lp_mm/       # 话题 3：组 LP 池子 (Uniswap V3 / HyperSwap) 主动量化与协同防御
│   ├── core/                        # GLFT 保留价格驱动的非对称极窄动态流动性区间挂载
│   ├── hedging/                     # 链上代币化现货 vs CEX/Perp Delta 实时动态对冲
│   └── backtest/                    # LVR 租金损耗消除验证与对冲费率套利回测
├── shared/                          # 共享底层核心数学模块
│   ├── glft_engine/                 # Stationary GLFT 求解器、到达强度与指数衰减校准器
│   ├── toxic_gate/                  # OFI 订单流不平衡、微观价格与短周期逆向冲击预测
│   └── data_lake_client/            # W:\database 数据湖客户端 (严格遵循 Left-Open / Right-Close 规约)
├── scripts/                         # 自动化数据拉取、清洗对齐与治理标注流水线
│   ├── ingest_cryptohftdata_lake.py # 从 CryptoHFTData 拉取 L2/Trades 治理写入 W:\database
│   └── verify_lake_governance.py    # 数据湖合规审查器 (严格审查毫秒时间戳与因果性)
└── README.md
```

---

## 二、 核心理论演进：结合 LUKE777 实战笔记的数学重构

### 1. 永续合约时间视界重构：用 GLFT 替代 A-S 的 $T-t$ 震荡
- **原版 A-S 致命缺陷**：古典保留价格 $r(s, q, t) = s - q \gamma \sigma^2 (T - t)$ 严重依赖剩余期限 $(T-t)$。永续合约没有到期日，若人为设 $T=1\text{天}$，临近 $t \to T$ 时库存惩罚项归零，策略会在持仓上限附近突然收窄利差、停止防守，导致被知情大单连续击穿。
- **GLFT (Guéant-Tapia-Manziadi) 稳态解改造**：
  采用无限视界极限下的稳态方程（Stationary GLFT），剔除对特定到期时间的依赖：
  $$r(s, q) = P_{\text{micro}} - q \cdot \gamma \cdot \sigma^2 \cdot \lambda_{\text{inv}}$$
  最优半利差：
  $$\delta^a(q) = \frac{1}{\gamma} \ln \left(1 + \frac{\gamma}{k_a}\right) + \frac{(2q + 1) \gamma \sigma^2}{2 k_a A_a}, \quad \delta^b(q) = \frac{1}{\gamma} \ln \left(1 + \frac{\gamma}{k_b}\right) - \frac{(2q - 1) \gamma \sigma^2}{2 k_b A_b}$$
  其中：
  - $A_a, A_b$：贴中间价时的瞬时到达强度（每秒被吃单次数）；
  - $k_a, k_b$：挂单距离盘口每拉远 $1\text{ bps}$ 时的成交概率指数衰减率；
  - $\lambda_{\text{inv}}$：库存敏感度调节因子，在 $|q| \to q_{\max}$ 时非线性放大。

### 2. 手续费内生化硬门槛 (Fee-Endogenous Hurdle)
- **拒绝事后扣减**：手续费必须作为底盘约束直接内生化到报价公式中：
  $$\delta_{\text{half}} \ge \max\left(\delta_{\text{GLFT}}, \text{Fee}_{\text{maker}} \times 2.5, \text{Min\_Tick}\right)$$
  对 OKX 普通账户（双边 $4.0\text{ bps}$）或 Hyperliquid（单边 $1.5\text{ bps}$），单边半利差必须至少留足 $3.5 \sim 5.0\text{ bps}$ 毛利空间，一旦无法覆盖直接停止挂单。

### 3. 防御坚不可摧三大工程防线
1. **VPIN (Volume-Synchronized Probability of Toxicity) 熔断**：将成交量划分为等额 Volume Bucket（如每 50 笔成交为一个 Bucket），当 VPIN $> 0.70$ 时判定主力大额下砸/插针，直接撤回所有挂单，进入 30 秒观察期。
2. **5 秒单边成交冷却**：单侧成交后立即冻结同向报单，仅挂反向平仓单，坚决防止在单边暴跌中连续接飞刀。
3. **库存硬箱体与斜率突变**：持仓达到最大库存 60% 时，保留价格向下偏置斜率放大 3 倍；达到 80% 时，强制停止顺向开仓，全力以 Post-Only Maker 平仓出清。

---

## 三、 数据湖接入与严苛治理标准 (`W:\database`)

本项目全部训练与回测数据对接本地集中式数据湖 `W:\database`，严格遵守 `GOVERNANCE_TIMESTAMP_ALIGNMENT.md` 规约：
1. **Left-Open 与 Right-Close 因果对齐**：Parquet 文件中若使用左开盘时间，在导入 NautilusTrader 事件驱动引擎时严格执行 $T_{\text{event\_close}} = T_{\text{parquet\_left\_open}} + \Delta T_{\text{bar}}$ 平移，彻底杜绝前瞻泄漏（Lookahead Bias）。
2. **微观数据源对齐**：通过 CryptoHFTData 与自建录制器，将毫秒级 Level-2 订单簿快照（books5 / 20 档）与 Tick Trades、资金费率严格按交易所时间戳与序列号对齐。
3. **治理准入标记**：只有通过 `verify_lake_governance.py` 严格校验（无时间倒流、无缺失、无交叉盘）的数据集，方可在元数据清单 `manifest_governance.json` 中标记为 `APPROVED_FOR_RESEARCH`。
