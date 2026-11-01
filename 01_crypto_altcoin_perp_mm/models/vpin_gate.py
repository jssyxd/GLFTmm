import time
import numpy as np
from collections import deque

class VPINCalculator:
    """
    Volume-Synchronized Probability of Toxicity (VPIN) 计算器
    将成交流切分为固定 Volume Bucket，量化逆向知情交易概率。
    """
    def __init__(self, bucket_volume: float = 1000.0, num_buckets: int = 50, vpin_threshold: float = 0.70):
        self.bucket_volume = bucket_volume
        self.num_buckets = num_buckets
        self.vpin_threshold = vpin_threshold
        
        self.current_buy_vol = 0.0
        self.current_sell_vol = 0.0
        self.current_total_vol = 0.0
        
        self.order_imbalances = deque(maxlen=num_buckets)
        self.current_vpin = 0.0

    def update_trade(self, price: float, size: float, side: str) -> float:
        """
        输入逐笔成交 (Tick Trade)，更新 VPIN 桶
        """
        vol = size
        is_buy = (side.lower() in ['buy', 'bid'])
        
        while vol > 0:
            remaining_bucket = self.bucket_volume - self.current_total_vol
            fill = min(vol, remaining_bucket)
            
            if is_buy:
                self.current_buy_vol += fill
            else:
                self.current_sell_vol += fill
                
            self.current_total_vol += fill
            vol -= fill
            
            # 当 Volume Bucket 填满时封闭该桶并计算失衡
            if self.current_total_vol >= self.bucket_volume:
                imbalance = abs(self.current_buy_vol - self.current_sell_vol)
                self.order_imbalances.append(imbalance)
                
                # 重置当前桶
                self.current_buy_vol = 0.0
                self.current_sell_vol = 0.0
                self.current_total_vol = 0.0
                
                # 计算全窗口滚动 VPIN
                if len(self.order_imbalances) == self.num_buckets:
                    total_imbalance = sum(self.order_imbalances)
                    self.current_vpin = total_imbalance / (self.num_buckets * self.bucket_volume)

        return self.current_vpin

    def is_triggered(self) -> bool:
        """判断是否击穿 VPIN 熔断阈值 (>= 0.70)"""
        return self.current_vpin >= self.vpin_threshold


class DefensiveCooldownManager:
    """
    5 秒单边成交冷却与方向冻结管理器
    单侧成交后立即冻结同向开仓单，仅允许挂反向平仓单，杜绝单边下砸接飞刀。
    """
    def __init__(self, cooldown_seconds: float = 5.0):
        self.cooldown_seconds = cooldown_seconds
        self.last_bid_fill_time = 0.0
        self.last_ask_fill_time = 0.0

    def on_fill(self, side: str, fill_time: float = None):
        now = fill_time if fill_time is not None else time.time()
        if side.lower() in ['buy', 'bid']:
            self.last_bid_fill_time = now
        elif side.lower() in ['sell', 'ask']:
            self.last_ask_fill_time = now

    def can_quote_bid(self, current_time: float = None) -> bool:
        now = current_time if current_time is not None else time.time()
        # 买单成交后 5 秒内冻结同向买入开仓 (BID_CUT)
        return (now - self.last_bid_fill_time) >= self.cooldown_seconds

    def can_quote_ask(self, current_time: float = None) -> bool:
        now = current_time if current_time is not None else time.time()
        # 卖单成交后 5 秒内冻结同向卖出开仓 (ASK_CUT)
        return (now - self.last_ask_fill_time) >= self.cooldown_seconds
