# Herdr Swarm Decisions Log

## 2026-11-05 - Decision 01: Architecture of MarketRegimeSentinel & MicroAggregator
- **Problem**: Fixed `min_spread_bps` (e.g. 12/14 bps) fails to account for market microstructure volatility and fee hurdles. Ordinary OKX accounts pay 4 bps round-trip maker fees.
- **Decision**: 
  1. Add `MarketRegimeSentinel` to `shared/sentinel/market_regime_sentinel.py` in `GLFTmm`. It monitors rolling L2 books, calculates OBI, OFI, Garman-Klass volatility, and enforces dynamic `min_spread_bps >= max(Base, 2.5 * MakerFee + alpha * sigma + beta * E[AdverseSelection])`.
  2. Add `MicroAggregator` in `shared/aggregator/micro_aggregator.py` that aggregates 1-second book snapshots to Parquet (3MB/day) for low-overhead transmission and Kaggle retraining.
  3. Add `kaggle_pipeline/kaggle_trainer_bridge.py` supporting rolling model update triggers using token `KGAT_ac40707589c873af896d44997f6c0d40` and CPU-only training.
- **Contract**:
  - `MarketRegimeSentinel.evaluate_regime(symbol, book_snapshot, trades)` -> `Dict[str, Any]` with `status`, `allow_quote`, `recommended_min_spread_bps`.
  - Zero disruption to existing `run_ake_v2.py`, `run_recall_v2.py`, and `hyperliquid_sndk_live.py`. Integration wraps existing `depth_sentinel` checks.
