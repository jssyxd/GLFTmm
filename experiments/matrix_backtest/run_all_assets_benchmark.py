"""
全市场全资产矩阵因果回测引擎 (涵盖 12 大核心标的):
1. HIP-3 标的: XYZ:NVDA, XYZ:XYZ100, XYZ:SP500, XYZ:GOLD, XYZ:BRENTOIL, XYZ:MU, XYZ:SKHY, XYZ:SNDK, XYZ:STRC
2. OKX 宽点差老牌标的: CORE-USDT-SWAP, WOO-USDT-SWAP, CHZ-USDT-SWAP, LTC-USDT-SWAP

严格执行《量化研发总宪法》黄金测试规约：
- 严格左开右闭因果撮合 (第 i 个 tick 报价，第 i+1 个 tick 外部击穿成交)
- 严禁同 Bar 穿越，严禁 bfill
- 样本内 (60%) vs 样本外 (40% 含周末休市期与盘中交替期)
- 策略矩阵对比：PURE_AS vs PURE_GLFT vs DEFENSIVE_GLFT
- 导出逐笔成交流水与总表
"""

import os
import sys
import json
import numpy as np
import pandas as pd

sys.path.insert(0, r"W:\GLFTmm")
from shared.toxic_gate.causal_features import load_clean_parquet, compute_causal_micro_features
from experiments.matrix_backtest.matrix_engine import run_causal_simulation

OUTPUT_DIR = r"W:\GLFTmm\experiments\matrix_backtest\results_all_assets"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 标的矩阵配置：(exchange, symbol, base_half_spread_bps, maker_fee_rate)
ALL_TARGETS = [
    # 话题 2: Hyperliquid HIP-3 RWA (8 个核心资产 + NVDA)
    ("hyperliquid_futures", "XYZ_NVDA", 4.0, 0.00015),
    ("hyperliquid_futures", "XYZ_XYZ100", 2.0, 0.00015),
    ("hyperliquid_futures", "XYZ_SP500", 2.0, 0.00015),
    ("hyperliquid_futures", "XYZ_GOLD", 3.0, 0.00015),
    ("hyperliquid_futures", "XYZ_BRENTOIL", 3.0, 0.00015),
    ("hyperliquid_futures", "XYZ_MU", 5.0, 0.00015),
    ("hyperliquid_futures", "XYZ_SKHY", 6.0, 0.00015),
    ("hyperliquid_futures", "XYZ_SNDK", 6.0, 0.00015),
    ("hyperliquid_futures", "XYZ_STRC", 8.0, 0.00015),
    # 话题 4: OKX 宽点差及老牌稳健标的
    ("okx_futures", "CORE-USDT-SWAP", 7.0, 0.0002),
    ("okx_futures", "WOO-USDT-SWAP", 4.0, 0.0002),
    ("okx_futures", "CHZ-USDT-SWAP", 4.0, 0.0002),
    ("okx_futures", "LTC-USDT-SWAP", 3.0, 0.0002)
]

def run_all_assets_benchmark():
    all_summary = {}

    for ex, sym, half_bps, fee in ALL_TARGETS:
        print(f"\n=======================================================")
        print(f"🚀 Benchmark: {ex} / {sym} (Spread: {half_bps*2} bps, Fee: {fee*10000} bps)")
        print(f"=======================================================")

        raw_df = load_clean_parquet(ex, sym, "trades")
        if raw_df.empty or len(raw_df) < 200:
            print(f"[SKIP] Insufficient data for {ex}/{sym}: {len(raw_df)} rows")
            continue

        print(f"Loaded {len(raw_df)} causal trade ticks for {sym}")
        feat_df = compute_causal_micro_features(raw_df)

        # 60% IS, 40% OOS
        split_idx = int(len(feat_df) * 0.60)
        df_is = feat_df.iloc[:split_idx].reset_index(drop=True)
        df_oos = feat_df.iloc[split_idx:].reset_index(drop=True)

        sym_result = {"in_sample": {}, "out_of_sample": {}}

        for mode in ["PURE_AS", "PURE_GLFT", "DEFENSIVE_GLFT_MATRIX"]:
            res_is = run_causal_simulation(df_is, mode=mode, maker_fee_rate=fee, half_spread_bps=half_bps)
            res_oos = run_causal_simulation(df_oos, mode=mode, maker_fee_rate=fee, half_spread_bps=half_bps)

            trades_is = res_is.pop("trades", [])
            trades_oos = res_oos.pop("trades", [])

            sym_result["in_sample"][mode] = res_is
            sym_result["out_of_sample"][mode] = res_oos

            # 保存 OOS 逐笔成交记录
            oos_file = os.path.join(OUTPUT_DIR, f"{sym}_{mode}_oos_trades.csv")
            if trades_oos:
                pd.DataFrame(trades_oos).to_csv(oos_file, index=False)

            print(f"  [{mode:22}] IS Net: {res_is['final_pnl']:.4f} (Win: {res_is['win_rate']:.1f}%) | OOS Net: {res_oos['final_pnl']:.4f} (Win: {res_oos['win_rate']:.1f}%) | Trades: {res_oos['num_trades']}")

        all_summary[sym] = sym_result

    summary_file = os.path.join(OUTPUT_DIR, "all_assets_matrix_summary.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(all_summary, f, indent=2, ensure_ascii=False)

    print(f"\n✅ All 12 Assets Backtested! Master summary saved to {summary_file}")
    return all_summary

if __name__ == "__main__":
    run_all_assets_benchmark()
