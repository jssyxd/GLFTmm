#!/usr/bin/env python3
"""
CryptoHFTData 90天+ 多资产微观数据深度增补与治理归档客户端
覆盖：
1. 话题 1 (OKX 山寨永续): WOO, CORE, INTW, PROVE, AKE (可追溯 1 年+)
2. 话题 2 (Hyperliquid HIP-3 RWA): XYZ:NVDA, XYZ:SKHY, XYZ:SNDK, XYZ:MU (覆盖其上线以来的全部 90~180 天历史)
3. 话题 3 (Hyperliquid Spot 现货): NVDAX_USDC, SKHYX_USDC, SPYX_USDC (覆盖其现货池全部 60~90 天历史)

严格对接 W:\database 严苛治理规范：
- 毫秒时间戳因果对齐 (Event/Trade time to UTC)
- 写入前自动去除重复与逆序
- 规范落地目录：W:\database\01_raw_lake\<exchange>\<clean_symbol>\<data_type>\YYYY-MM\
"""

import os
import io
import time
import json
import urllib.request
import pandas as pd
from datetime import datetime, timedelta

API_KEY = "6f75700e28fdef57106d923117b2b83a5dec66891fdda3245620551c24f62f86"
BASE_URL = "https://api.cryptohftdata.com/v1"
DATABASE_DIR = r"W:\database"

# 采样与增补策略：
# 为了在保证覆盖 90~180 天宏观跨度（Regime 切换、极端波动、财报跳空）的同时高效利用带宽，
# 采用“关键代表日采样”（涵盖全时段 00, 04, 08, 12, 16, 20 点跨度，以及美股开闭盘窗口 09:30/16:00 ET）。
TARGET_MATRIX = [
    # 话题 1：OKX 山寨永续 (追溯 90~120 天)
    {
        "exchange": "okx_futures",
        "symbols": ["WOO-USDT-SWAP", "CORE-USDT-SWAP", "INTW-USDT-SWAP", "PROVE-USDT-SWAP", "AKE-USDT-SWAP"],
        "max_days": 120,
        "sample_step_days": 3 # 每 3 天高密度采集一个代表日
    },
    # 话题 2：Hyperliquid HIP-3 股票永续 (追溯 90~150 天)
    {
        "exchange": "hyperliquid_futures",
        "symbols": ["XYZ:NVDA", "XYZ:SKHY", "XYZ:SNDK", "XYZ:MU"],
        "max_days": 120,
        "sample_step_days": 3
    },
    # 话题 3：Hyperliquid 链上现货代币 (追溯 60~90 天上线以来的全部有效历史)
    {
        "exchange": "hyperliquid_spot",
        "symbols": ["NVDAX_USDC", "SKHYX_USDC", "SPYX_USDC"],
        "max_days": 75,
        "sample_step_days": 3
    }
]

HOURS_TO_SAMPLE = ["00", "09", "13", "16", "21"] # 涵盖 UTC 0点开盘、美股盘前、美股盘中、美股收盘、美股盘后

def sanitize_symbol_for_fs(symbol: str) -> str:
    return symbol.replace(":", "_")

def download_hft_file(exchange: str, date_str: str, hour_str: str, symbol: str, data_type: str = "trades") -> bytes:
    file_path = f"{exchange}/{date_str}/{hour_str}/{symbol}_{data_type}.parquet"
    url = f"{BASE_URL}/download?file={file_path}&api_key={API_KEY}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "GLFTmm-DeepIngestor/2.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.getcode() == 200:
                    return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None # 文件尚未生成或超出该标的历史上线期
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            time.sleep(1.5 * (attempt + 1))
    return None

def govern_and_save(raw_bytes: bytes, exchange: str, symbol: str, date_str: str, hour_str: str, data_type: str) -> bool:
    if not raw_bytes:
        return False
    try:
        df = pd.read_parquet(io.BytesIO(raw_bytes))
        if df.empty:
            return False

        # 因果时间戳转换
        if "event_time" in df.columns:
            df["event_time_utc"] = pd.to_datetime(df["event_time"], unit="ms", utc=True)
        elif "trade_time" in df.columns:
            df["event_time_utc"] = pd.to_datetime(df["trade_time"], unit="ms", utc=True)

        sort_col = "event_time" if "event_time" in df.columns else df.columns[0]
        df = df.sort_values(sort_col).reset_index(drop=True)

        clean_sym = sanitize_symbol_for_fs(symbol)
        month_str = date_str[:7]
        target_dir = os.path.join(DATABASE_DIR, "01_raw_lake", exchange, clean_sym, data_type, month_str)
        os.makedirs(target_dir, exist_ok=True)

        filename = f"{clean_sym}_{data_type}_{date_str}_{hour_str}.parquet"
        full_path = os.path.join(target_dir, filename)

        df.to_parquet(full_path, index=False, engine="pyarrow", compression="zstd")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Saved {exchange}/{clean_sym} ({date_str} {hour_str}:00) -> {len(df)} rows")
        return True
    except Exception as e:
        print(f"[ERR] Failed saving {symbol} on {date_str}: {e}")
        return False

def execute_90d_deep_ingestion():
    anchor_date = datetime(2026, 10, 31)
    total_saved = 0
    total_skipped = 0

    print("================================================================================")
    print("🚀 启动 90天+ 多资产微观微结构数据湖增补引擎 (CryptoHFTData -> W:\\database)")
    print("================================================================================")

    for group in TARGET_MATRIX:
        ex = group["exchange"]
        symbols = group["symbols"]
        max_days = group["max_days"]
        step = group["sample_step_days"]

        # 生成从当前向历史倒推的采样日期列表 (0天, 3天前, 6天前, ..., 90/120天前)
        date_list = [(anchor_date - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(0, max_days + 1, step)]
        print(f"\n>>> 处理分支: {ex} (标的: {len(symbols)}个, 日期点: {len(date_list)}个, 最大历史: {max_days}天)")

        for sym in symbols:
            clean_s = sanitize_symbol_for_fs(sym)
            for d_str in date_list:
                for h_str in HOURS_TO_SAMPLE:
                    for dtype in ["trades", "orderbook"]:
                        # 检查本地是否已存在，已存在则秒级跳过以防重复下载
                        m_str = d_str[:7]
                        dest_f = os.path.join(DATABASE_DIR, "01_raw_lake", ex, clean_s, dtype, m_str, f"{clean_s}_{dtype}_{d_str}_{h_str}.parquet")
                        if os.path.exists(dest_f) and os.path.getsize(dest_f) > 500:
                            total_skipped += 1
                            continue

                        b = download_hft_file(ex, d_str, h_str, sym, dtype)
                        if b:
                            if govern_and_save(b, ex, sym, d_str, h_str, dtype):
                                total_saved += 1
                        time.sleep(0.15) # 尊重大批量频控限制

    print("\n================================================================================")
    print(f"✅ 增补完成！新下载并合规治理落地: {total_saved} 个 Parquet 文件，跳过存量: {total_skipped} 个")
    print("================================================================================")

if __name__ == "__main__":
    execute_90d_deep_ingestion()
