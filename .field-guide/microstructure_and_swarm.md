# Field Guide: High-Frequency Market Making & Herdr Swarm Invariants

### 2026-11-05: OKX Maker Fee Polarity
- OKX API `fee` field: negative value means user expense (deducted fee). Positive value means maker rebate.
- Standard Lv1 maker fee is 2.0 bps (0.02%). Dual maker round-trip is 4.0 bps.
- Min spread hurdle must strictly satisfy: `min_spread_bps >= 2.5 * total_fees = 10.0 bps`.

### 2026-11-05: Kaggle API & Resource Limits
- Never request GPU for tabular microstructure LightGBM training. Kaggle CPU (4 cores, 30GB RAM) is unlimited and runs in 60-120 seconds.
- Compress raw ticks to 1s micro-features before transport: reduces 100MB+ data to ~3MB Parquet.
