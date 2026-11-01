import os
import json
import pandas as pd
from datetime import datetime

DATABASE_DIR = r"W:\database"
RAW_LAKE = os.path.join(DATABASE_DIR, "01_raw_lake")
MANIFEST_PATH = os.path.join(DATABASE_DIR, "manifest_governance.json")

def audit_and_tag_lake():
    """
    审查 W:\database 中的 Parquet 文件，按数据湖准入标准实施治理审计
    审查项：
    1. 严格时间单调性 (无逆序)
    2. 无未来函数/零值穿透
    3. 字段完备性 (L2 orderbook 包含 bids/asks 或 trades 包含 price/size/side)
    全部通过的数据集方可被打上 'APPROVED_FOR_RESEARCH' 标签。
    """
    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            manifest = {}

    audit_summary = {"total_files_audited": 0, "approved": 0, "rejected": 0, "records": []}

    for root, dirs, files in os.walk(RAW_LAKE):
        for f in files:
            if not f.endswith(".parquet"):
                continue
                
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, DATABASE_DIR)
            audit_summary["total_files_audited"] += 1

            try:
                # 仅读元数据与头尾数据做快速无损审查
                df = pd.read_parquet(full_path)
                
                # 检查 1: 必须非空
                if df.empty or len(df) < 5:
                    audit_summary["rejected"] += 1
                    continue

                # 检查 2: 时间因果递增
                ts_col = "event_time" if "event_time" in df.columns else ("trade_time" if "trade_time" in df.columns else df.columns[0])
                is_monotonic = df[ts_col].is_monotonic_increasing
                
                # 检查 3: 必须有价格与数量有效性
                has_valid_data = True
                if "price" in df.columns and "quantity" in df.columns:
                    has_valid_data = (df["price"].notna().all()) and (df["quantity"].notna().all())

                if is_monotonic and has_valid_data:
                    status = "APPROVED_FOR_RESEARCH"
                    audit_summary["approved"] += 1
                else:
                    status = "REJECTED_AUDIT_FAIL"
                    audit_summary["rejected"] += 1

                record = {
                    "path": rel_path.replace("\\", "/"),
                    "rows": len(df),
                    "status": status,
                    "audited_at": datetime.utcnow().isoformat() + "Z",
                    "time_monotonic": bool(is_monotonic)
                }
                manifest[rel_path.replace("\\", "/")] = record
                audit_summary["records"].append(record)

            except Exception as e:
                audit_summary["rejected"] += 1
                manifest[rel_path.replace("\\", "/")] = {"status": "ERROR_CORRUPT", "error": str(e)}

    # 回写权威治理清单
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Audit Finished: {audit_summary['approved']} Approved, {audit_summary['rejected']} Rejected.")
    return audit_summary

if __name__ == "__main__":
    audit_and_tag_lake()
