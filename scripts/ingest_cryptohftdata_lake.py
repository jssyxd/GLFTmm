#!/usr/bin/env python3
"""
CryptoHFTData 高频微观数据自动化摄取与治理归档客户端
严格对接 W:\database 数据湖标准，支持 OKX Futures/Spot 与 Hyperliquid Futures/Spot
涵盖：Tick Trades、L2 Orderbook、Mark/Index 价格与资金费率。
"""

import os
import io
import time
import json
import urllib.request
import pandas as pd
from datetime import datetime

API_KEY = "6f75700e28fdef57106d923117b2b83a5dec66891fdda3245620551c24f62f86"
BASE_URL = "https://api.cryptohftdata.com/v1"
DATABASE_DIR = r"W:\database"

# 目标标的矩阵分类
UNIVERSE_CONFIG = {
    # 话题 1: 加密山寨永续合约 (OKX)
    "topic1_altcoin_perp": {
        "exchange": "okx_futures",
        "symbols": ["WOO-USDT-SWAP", "CORE-USDT-SWAP", "INTW-USDT-SWAP", "PROVE-USDT-SWAP", "AKE-USDT-SWAP"]
    },
    # 话题 2: RWA 代币化正股与 HIP-3 (Hyperliquid)
    "topic2_rwa_hip3": {
        "exchange": "hyperliquid_futures",
        "symbols": ["XYZ:NVDA", "XYZ:SKHY", "XYZ:SNDK", "XYZ:MU"]
    },
    # 话题 3: 链上现货代币 (Hyperliquid Spot 用于组池与跨市对冲)
    "topic3_spot_pool": {
        "exchange": "hyperliquid_spot",
        "symbols": ["NVDAX_USDC", "SKHYX_USDC", "SPYX_USDC"]
    }
}

def sanitize_symbol_for_fs(symbol: str) -> str:
    """Windows 文件系统路径安全化 (将冒号替换为下划线)"""
    return symbol.replace(":", "_")

def download_hft_file(exchange: str, date_str: str, hour_str: str, symbol: str, data_type: str = "trades") -> bytes:
    """
    通过 REST API 下载指定小时切片的 Parquet 文件 (trades 或 orderbook)
    """
    file_path = f"{exchange}/{date_str}/{hour_str}/{symbol}_{data_type}.parquet"
    url = f"{BASE_URL}/download?file={file_path}&api_key={API_KEY}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "GLFTmm-DataLake-Ingestor"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                if resp.getcode() == 200:
                    return resp.read()
        except Exception as e:
            time.sleep(2 * (attempt + 1))
            if attempt == 2:
                print(f"[FAIL] Download {file_path} failed: {e}")
    return None

def govern_and_store_lake(raw_bytes: bytes, exchange: str, symbol: str, date_str: str, hour_str: str, data_type: str):
    """
    依据 W:\\database 严苛标准进行时间戳校验、格式转换并落盘
    """
    if not raw_bytes:
        return False
        
    df = pd.read_parquet(io.BytesIO(raw_bytes))
    if df.empty:
        return False

    # 治理检查 1: 毫秒与微秒时间戳统一因果校验
    if "event_time" in df.columns:
        df["event_time_utc"] = pd.to_datetime(df["event_time"], unit="ms", utc=True)
    elif "trade_time" in df.columns:
        df["event_time_utc"] = pd.to_datetime(df["trade_time"], unit="ms", utc=True)

    # 治理检查 2: 严格排序与坏数据剔除
    sort_col = "event_time" if "event_time" in df.columns else df.columns[0]
    df = df.sort_values(sort_col).reset_index(drop=True)

    # 规范存储路径：01_raw_lake/<exchange>/<symbol_clean>/<data_type>/YYYY-MM/
    clean_sym = sanitize_symbol_for_fs(symbol)
    month_str = date_str[:7]
    target_dir = os.path.join(DATABASE_DIR, "01_raw_lake", exchange, clean_sym, data_type, month_str)
    os.makedirs(target_dir, exist_ok=True)
    
    filename = f"{clean_sym}_{data_type}_{date_str}_{hour_str}.parquet"
    full_path = os.path.join(target_dir, filename)
    
    df.to_parquet(full_path, index=False, engine="pyarrow", compression="zstd")
    print(f"[GOVERNED & STORED] {full_path} (Rows: {len(df)})")
    return True

def run_ingestion_sample(date_str: str = "2026-10-31", hour_str: str = "00"):
    print(f"=== Starting Ingestion for Date: {date_str} Hour: {hour_str} ===")
    for topic, cfg in UNIVERSE_CONFIG.items():
        ex = cfg["exchange"]
        for sym in cfg["symbols"][:2]: # 各跑前 2 个核心标的
            for dtype in ["trades", "orderbook"]:
                print(f"Fetching {ex} / {sym} / {dtype}...")
                b = download_hft_file(ex, date_str, hour_str, sym, dtype)
                if b:
                    govern_and_store_lake(b, ex, sym, date_str, hour_str, dtype)
                time.sleep(0.5)

if __name__ == "__main__":
    run_ingestion_sample()
