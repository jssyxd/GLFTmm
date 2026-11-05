# -*- coding: utf-8 -*-
"""
test_kaggle_bridge.py - Unit tests for KaggleTrainerBridge
"""

import os
import json
import tempfile
import pytest
import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from kaggle_pipeline.kaggle_trainer_bridge import KaggleTrainerBridge

def test_kaggle_bridge_init():
    bridge = KaggleTrainerBridge(username="test_user", api_token="KGAT_ac40707589c873af896d44997f6c0d40")
    assert bridge.username == "test_user"
    assert bridge.api_token == "ac40707589c873af896d44997f6c0d40" # stripped prefix

def test_kernel_metadata_cpu_only():
    bridge = KaggleTrainerBridge()
    meta = bridge.generate_kernel_metadata("kernel.py")
    assert meta["enable_gpu"] is False # Must not burn GPU hours
    assert meta["enable_internet"] is True

def test_offline_calibration():
    bridge = KaggleTrainerBridge()
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".jsonl") as f:
        for s in [10.0, 12.0, 14.0, 16.0, 18.0]:
            f.write(json.dumps({"spread_bps": s}) + "\n")
        f_name = f.name
        
    try:
        calib = bridge.simulate_offline_calibration(f_name)
        assert "A_arrival" in calib
        assert "k_decay" in calib
        assert calib["recommended_min_spread_bps"] >= 10.0
    finally:
        os.remove(f_name)

if __name__ == '__main__':
    pytest.main([__file__, "-v"])
