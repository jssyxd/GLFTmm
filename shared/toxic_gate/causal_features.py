import os
import glob
import pandas as pd
import numpy as np

DATABASE_DIR = r"W:\database\01_raw_lake"

def load_clean_parquet(exchange: str, symbol: str, data_type: str = "trades") -> pd.DataFrame:
    pattern = os.path.join(DATABASE_DIR, exchange, symbol, data_type, "*", "*.parquet")
    files = sorted(glob.glob(pattern))
    if not files:
        print(f"[WARN] No files found for {exchange} {symbol} {data_type} with pattern {pattern}")
        return pd.DataFrame()
    
    dfs = []
    for f in files:
        try:
            df = pd.read_parquet(f)
            dfs.append(df)
        except Exception as e:
            print(f"[ERR] Error reading {f}: {e}")
            
    if not dfs:
        return pd.DataFrame()
        
    full_df = pd.concat(dfs, ignore_index=True)
    
    # 严格时间因果排序与纳秒/毫秒统一
    ts_col = "event_time" if "event_time" in full_df.columns else ("trade_time" if "trade_time" in full_df.columns else full_df.columns[0])
    full_df["ts_ms"] = full_df[ts_col].astype(np.int64)
    # 如果是微秒或纳秒，折合到毫秒
    if full_df["ts_ms"].iloc[0] > 1e15: # 纳秒
        full_df["ts_ms"] = full_df["ts_ms"] // 1_000_000
    elif full_df["ts_ms"].iloc[0] > 1e12: # 微秒
        full_df["ts_ms"] = full_df["ts_ms"] // 1_000

    full_df = full_df.sort_values("ts_ms").reset_index(drop=True)
    full_df["price"] = full_df["price"].astype(float)
    full_df["quantity"] = full_df["quantity"].astype(float)
    if "is_buyer_maker" in full_df.columns:
        full_df["side"] = np.where(full_df["is_buyer_maker"], "sell", "buy") # 如果 buyer 是 maker，说明主动方是 sell
    return full_df

def compute_causal_micro_features(trades_df: pd.DataFrame, window_ticks: int = 50) -> pd.DataFrame:
    """
    计算完全因果的微观特征：绝对禁止 bfill()，绝对禁止使用当前与未来事件
    """
    df = trades_df.copy()
    # 1. 滚动订单流不平衡 (Order Flow Imbalance, OFI)
    buy_vol = np.where(df["side"] == "buy", df["quantity"], 0.0)
    sell_vol = np.where(df["side"] == "sell", df["quantity"], 0.0)
    
    df["rolling_buy_vol"] = pd.Series(buy_vol).rolling(window_ticks, min_periods=1).sum()
    df["rolling_sell_vol"] = pd.Series(sell_vol).rolling(window_ticks, min_periods=1).sum()
    df["ofi"] = (df["rolling_buy_vol"] - df["rolling_sell_vol"]) / (df["rolling_buy_vol"] + df["rolling_sell_vol"] + 1e-6)
    
    # 2. 纯因果滚动波动率 (基于对数收益率)
    log_ret = np.log(df["price"] / df["price"].shift(1)).fillna(0.0)
    df["rolling_vol"] = log_ret.rolling(window_ticks, min_periods=5).std().fillna(0.0001)
    
    # 3. 严格因果标签：未来 10 个 tick 的价格漂移方向 (仅用于离线验证，严禁回传给在线决策)
    future_ret = (df["price"].shift(-10) - df["price"]) / df["price"]
    # 毒性定义：向不利方向穿透超过 5 bps
    df["is_toxic_flow"] = (np.abs(future_ret) > 0.0005).astype(int)
    
    return df
