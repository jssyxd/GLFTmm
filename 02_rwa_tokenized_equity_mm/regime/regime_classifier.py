import datetime
import pytz

class RWARegimeClassifier:
    """
    RWA 代币化正股做市状态机 (美股/韩股与 HIP-3 永续标的)
    识别传统交易所开收盘与周末自治状态，动态调制利差与基差均值回归。
    """
    REGIME_US_OPEN = 0       # 美股盘中：流动性好，Oracle 紧跟真实撮合，狭窄利差 (6~8 bps)
    REGIME_TRANSITION = 1    # 盘前盘后过渡期：流动性下降，利差提宽至 (15~25 bps)
    REGIME_WEEKEND_CLOSED = 2 # 周末全休市期：纯链上自治，利差扩大至 (40~80 bps)，注入基差均值回归

    def __init__(self, tz_name: str = "America/New_York"):
        self.tz = pytz.timezone(tz_name)

    def classify(self, utc_timestamp: float) -> int:
        dt_utc = datetime.datetime.fromtimestamp(utc_timestamp, pytz.utc)
        dt_local = dt_utc.astimezone(self.tz)

        weekday = dt_local.weekday() # 0: Mon, ..., 4: Fri, 5: Sat, 6: Sun

        # 周六、周日全天休市
        if weekday in (5, 6):
            return self.REGIME_WEEKEND_CLOSED

        # 周一至周五常规美股交易时间 (9:30 - 16:00 ET)
        minute_of_day = dt_local.hour * 60 + dt_local.minute
        open_time = 9 * 60 + 30
        close_time = 16 * 60

        if open_time <= minute_of_day < close_time:
            return self.REGIME_US_OPEN
        else:
            return self.REGIME_TRANSITION

    def is_monday_gap_risk_window(self, utc_timestamp: float) -> bool:
        """
        周一开盘跳空防插针检测 (Gap Defense)：
        在周一传统交易所开盘前 15 分钟 (9:15 - 9:30 ET) 触发全额撤单，规避跳空穿盘
        """
        dt_utc = datetime.datetime.fromtimestamp(utc_timestamp, pytz.utc)
        dt_local = dt_utc.astimezone(self.tz)

        if dt_local.weekday() == 0: # Monday
            minute_of_day = dt_local.hour * 60 + dt_local.minute
            if (9 * 60 + 15) <= minute_of_day < (9 * 60 + 30):
                return True
        return False
