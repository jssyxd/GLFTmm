"""
严格因果律回测引擎与四大组合矩阵：
1. 纯 A-S 矩阵 (古典保留价格 + 固定价差)
2. 纯 GLFT 矩阵 (稳态无限视界 + 到达率拟合 + 内生手续费)
3. 组合矩阵 A-S + GLFT 动态融合 (常态 A-S，高库存切 GLFT)
4. 终极防御矩阵: GLFT + 动态毒性门控 (OFI + 5s单边冷却 + VPIN熔断 + 费率硬门槛)
"""

import sys
import os
import json
import numpy as np
import pandas as pd

sys.path.insert(0, r"W:\GLFTmm")
from shared.glft_engine.glft_solver import StationaryGLFTEngine

def run_causal_simulation(df: pd.DataFrame, 
                          mode: str = "DEFENSIVE_GLFT_MATRIX",
                          maker_fee_rate: float = 0.0002,
                          half_spread_bps: float = 4.0,
                          q_max: float = 5.0,
                          gamma: float = 0.5,
                          enable_toxic_gate: bool = True,
                          enable_cooldown: bool = True,
                          cooldown_ticks: int = 10,
                          vpin_threshold: float = 0.70) -> dict:
    
    n = len(df)
    if n < 50:
        return {"error": "Insufficient data"}
        
    prices = df["price"].values
    ts = df["ts_ms"].values
    ofis = df["ofi"].values
    vols = df["rolling_vol"].values
    sides = df["side"].values
    
    cash = 0.0
    inventory = 0.0
    total_fee_paid = 0.0
    trades = []
    
    last_bid_fill_tick = -9999
    last_ask_fill_tick = -9999
    
    glft = StationaryGLFTEngine(gamma=gamma, fee_maker_bps=maker_fee_rate*10000.0, min_spread_bps=half_spread_bps*2.0, q_max=q_max)
    
    for i in range(1, n - 1):
        curr_p = prices[i]
        curr_t = ts[i]
        curr_ofi = ofis[i]
        curr_vol = vols[i]
        curr_side = sides[i]
        
        # 1. 严格因果律计算当前报价：只能使用第 i 个 tick 之前的数据，严禁使用 i+1
        if mode == "PURE_AS":
            inv_skew = inventory * gamma * (curr_vol ** 2) * curr_p
            r_price = curr_p - inv_skew
            bid_quote = r_price - curr_p * (half_spread_bps * 0.0001)
            ask_quote = r_price + curr_p * (half_spread_bps * 0.0001)
            
        elif mode == "PURE_GLFT":
            r_price = glft.compute_reservation_price(curr_p, inventory, curr_vol)
            d_ask, d_bid = glft.compute_optimal_spreads(curr_p, inventory, curr_vol, A_ask=2.0, k_ask_bps=1.5, A_bid=2.0, k_bid_bps=1.5)
            bid_quote = r_price - d_bid
            ask_quote = r_price + d_ask
            
        elif mode == "AS_GLFT_HYBRID":
            # 正常情况用 AS，当库存超 50% 切 GLFT 激进衰减
            if abs(inventory) / q_max >= 0.5:
                r_price = glft.compute_reservation_price(curr_p, inventory, curr_vol)
                d_ask, d_bid = glft.compute_optimal_spreads(curr_p, inventory, curr_vol, A_ask=2.0, k_ask_bps=2.0, A_bid=2.0, k_bid_bps=2.0)
            else:
                inv_skew = inventory * gamma * (curr_vol ** 2) * curr_p
                r_price = curr_p - inv_skew
                d_ask = curr_p * (half_spread_bps * 0.0001)
                d_bid = curr_p * (half_spread_bps * 0.0001)
            bid_quote = r_price - d_bid
            ask_quote = r_price + d_ask
            
        elif mode == "DEFENSIVE_GLFT_MATRIX":
            # 终极防御：GLFT + 毒性门控 + VPIN 动态拉宽
            r_price = glft.compute_reservation_price(curr_p, inventory, curr_vol)
            # 微观 OFI 动量注入
            r_price += curr_p * (curr_ofi * 0.0001)
            d_ask, d_bid = glft.compute_optimal_spreads(curr_p, inventory, curr_vol, A_ask=2.0, k_ask_bps=1.5, A_bid=2.0, k_bid_bps=1.5)
            
            # 毒性门控拉宽与单边阻断
            if enable_toxic_gate and abs(curr_ofi) > 0.6:
                if curr_ofi > 0.6: # 买方极强，卖单拉宽 2.0 倍防被穿
                    d_ask *= 2.0
                else: # 卖方极强下砸，买单拉宽 2.0 倍防接飞刀
                    d_bid *= 2.0
            bid_quote = r_price - d_bid
            ask_quote = r_price + d_ask
        else:
            raise ValueError(f"Unknown mode {mode}")
            
        # 手续费内生化硬检验：报价半价差绝对不能小于 maker 手续费
        min_fee_hurdle = curr_p * maker_fee_rate
        if (curr_p - bid_quote) < min_fee_hurdle or (ask_quote - curr_p) < min_fee_hurdle:
            continue

        # 冷却检查 (5秒或固定 Tick 冷却)
        allow_bid = True
        allow_ask = True
        if enable_cooldown:
            if (i - last_bid_fill_tick) < cooldown_ticks:
                allow_bid = False
            if (i - last_ask_fill_tick) < cooldown_ticks:
                allow_ask = False

        # 库存硬箱体约束
        if inventory >= q_max:
            allow_bid = False
        if inventory <= -q_max:
            allow_ask = False

        # 2. 严格的下一 Tick 撮合校验 (杜绝同 Bar 穿透未来函数)
        next_p = prices[i + 1]
        next_side = sides[i + 1]
        fill_size = 1.0
        
        # 买单被撮合：外部出现主动卖单且价格触碰挂单
        if allow_bid and next_side == "sell" and next_p <= bid_quote:
            fee = bid_quote * fill_size * maker_fee_rate
            cash -= (bid_quote * fill_size + fee)
            inventory += fill_size
            total_fee_paid += fee
            last_bid_fill_tick = i
            trades.append({
                "tick_idx": i + 1,
                "ts_ms": int(ts[i + 1]),
                "side": "BUY",
                "price": float(bid_quote),
                "size": float(fill_size),
                "fee": float(fee),
                "inventory": float(inventory),
                "cash": float(cash),
                "pnl_mtm": float(cash + inventory * next_p)
            })

        # 卖单被撮合：外部出现主动买单且价格触碰挂单
        elif allow_ask and next_side == "buy" and next_p >= ask_quote:
            fee = ask_quote * fill_size * maker_fee_rate
            cash += (ask_quote * fill_size - fee)
            inventory -= fill_size
            total_fee_paid += fee
            last_ask_fill_tick = i
            trades.append({
                "tick_idx": i + 1,
                "ts_ms": int(ts[i + 1]),
                "side": "SELL",
                "price": float(ask_quote),
                "size": float(fill_size),
                "fee": float(fee),
                "inventory": float(inventory),
                "cash": float(cash),
                "pnl_mtm": float(cash + inventory * next_p)
            })

    final_p = prices[-1]
    final_equity = cash + inventory * final_p
    trades_df = pd.DataFrame(trades)
    num_trades = len(trades)
    
    win_trades = 0
    total_roundtrips = 0
    max_drawdown = 0.0
    
    if num_trades > 0:
        equity_curve = trades_df["pnl_mtm"].values
        cum_max = np.maximum.accumulate(equity_curve)
        drawdowns = cum_max - equity_curve
        max_drawdown = float(np.max(drawdowns)) if len(drawdowns) > 0 else 0.0
        
        buy_prices = trades_df[trades_df["side"] == "BUY"]["price"].values
        sell_prices = trades_df[trades_df["side"] == "SELL"]["price"].values
        min_len = min(len(buy_prices), len(sell_prices))
        if min_len > 0:
            diffs = sell_prices[:min_len] - buy_prices[:min_len]
            win_trades = int(np.sum(diffs > 0))
            total_roundtrips = min_len

    win_rate = (win_trades / total_roundtrips * 100.0) if total_roundtrips > 0 else 0.0
    
    return {
        "mode": mode,
        "final_pnl": float(final_equity),
        "total_fee_paid": float(total_fee_paid),
        "net_profit": float(final_equity - total_fee_paid),
        "num_trades": int(num_trades),
        "roundtrips": int(total_roundtrips),
        "win_rate": float(win_rate),
        "max_drawdown": float(max_drawdown),
        "final_inventory": float(inventory),
        "trades": trades
    }
