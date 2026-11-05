# -*- coding: utf-8 -*-
"""
kaggle_trainer_bridge.py - Bridge to Trigger Kaggle Rolling Retraining & Weight Hot-Reload
Authenticates with Kaggle using user credentials and triggers headless CPU kernel execution.
Extracts calibrated GLFT parameters and LightGBM model weights upon completion.
"""

import os
import json
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional

DEFAULT_KAGGLE_USERNAME = "jssyxd"
DEFAULT_KAGGLE_KEY = "ac40707589c873af896d44997f6c0d40" # extracted from KGAT token

class KaggleTrainerBridge:
    def __init__(
        self,
        username: str = DEFAULT_KAGGLE_USERNAME,
        api_token: str = DEFAULT_KAGGLE_KEY,
        kernel_slug: str = "glft-rolling-microstructure-trainer"
    ):
        self.username = username
        self.api_token = api_token.replace("KGAT_", "") if api_token.startswith("KGAT_") else api_token
        self.kernel_slug = kernel_slug
        self.base_url = "https://www.kaggle.com/api/v1"
        self._setup_kaggle_credentials()

    def _setup_kaggle_credentials(self):
        """Ensure local kaggle.json exists for CLI and library compatibility."""
        kaggle_dir = os.path.expanduser("~/.kaggle")
        os.makedirs(kaggle_dir, exist_ok=True)
        kaggle_file = os.path.join(kaggle_dir, "kaggle.json")
        try:
            with open(kaggle_file, "w", encoding="utf-8") as f:
                json.dump({"username": self.username, "key": self.api_token}, f)
        except Exception:
            pass

    def generate_kernel_metadata(self, code_file: str, dataset_sources: list = None) -> Dict[str, Any]:
        """Generate CPU-only kernel metadata JSON."""
        return {
            "id": f"{self.username}/{self.kernel_slug}",
            "title": "GLFT Rolling Microstructure Trainer",
            "code_file": code_file,
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False, # STRICTLY FALSE: CPU-only, zero GPU quota consumption
            "enable_tpu": False,
            "enable_internet": True,
            "dataset_sources": dataset_sources or [],
            "competition_sources": [],
            "kernel_sources": []
        }

    def simulate_offline_calibration(self, micro_data_path: str) -> Dict[str, Any]:
        """
        Fast local fallback / offline simulation:
        Calibrate GLFT arrival rate A, decay kappa, and min spread hurdle from micro data.
        """
        if not os.path.exists(micro_data_path):
            return {
                "A_arrival": 3.85,
                "k_decay": 2.15,
                "recommended_min_spread_bps": 12.0,
                "timestamp": time.time()
            }
            
        spreads = []
        with open(micro_data_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    if "spread_bps" in d:
                        spreads.append(d["spread_bps"])
                except Exception:
                    continue
                    
        if not spreads:
            med_spread = 12.0
        else:
            spreads.sort()
            med_spread = spreads[len(spreads) // 2]
            
        # Calibrated parameters
        # Under normal L2 dynamics, arrival intensity A is inversely proportional to spread, k_decay scales with elasticity
        return {
            "A_arrival": round(max(1.0, 50.0 / max(med_spread, 5.0)), 3),
            "k_decay": round(max(0.8, med_spread * 0.18), 3),
            "recommended_min_spread_bps": round(max(10.0, med_spread * 0.95), 2),
            "timestamp": time.time()
        }
