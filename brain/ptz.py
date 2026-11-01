# -*- coding: utf-8 -*-
"""云台是共享资源：谁能转、转完怎么回来、转动期间哪些规则要暂停

  - 同一时间只允许一个转动指令（加锁），指令在 io 线程执行，不阻塞主循环。
  - 记录累计水平转动量；目标离开 return_sec 秒后，反向转回原位（近似复位，长期会有漂移，
    建议定期在米家 App 里校正一次）。
  - 转动期间及复位后 settle_sec 秒内，zones_valid=False：区域规则、热力图暂停，
    因为画面坐标已经不对应原来标定的区域。
"""
import threading, time


class PtzController:
    def __init__(self, enabled=False, speed=0.4, pulse=0.4, return_sec=60, settle_sec=5):
        self.enabled = enabled
        self.speed, self.pulse = speed, pulse
        self.return_sec, self.settle_sec = return_sec, settle_sec
        self._lock = threading.Lock()
        self.net = 0.0                # 累计水平转动（秒 × 方向）
        self.last_move = 0.0
        self.busy = False
        self.pending = False          # 已排队、还没执行完的转动指令

    def can_nudge(self, settle=1.0):
        """没有在转、没有排队，且上次转完已过 settle 秒（保证下一次用的是转动后的新画面）"""
        return (self.enabled and not self.busy and not self.pending
                and time.time() - self.last_move > settle)

    def request(self):
        """主循环排队前调用，防止同一个目标连续排进多条转动指令"""
        self.pending = True

    @property
    def zones_valid(self):
        return abs(self.net) < 1e-6 and not self.busy and time.time() - self.last_move > self.settle_sec

    def _move(self, direction, secs):
        from onvif_ptz import continuous_move, stop, get_profile_token
        with self._lock:
            self.busy = True
            try:
                tok = get_profile_token()
                continuous_move(tok, self.speed * direction, 0)
                time.sleep(secs)
                stop(tok)
                self.net += direction * secs
            finally:
                self.last_move = time.time()
                self.busy = False

    def nudge(self, dx_frac):
        """目标偏离画面中心 dx_frac（-0.5..0.5）时转一下；返回是否发出指令"""
        try:
            if not self.enabled or self.busy or abs(dx_frac) < 0.15:
                return False
            self._move(1 if dx_frac > 0 else -1, self.pulse)
            return True
        finally:
            self.pending = False

    def maybe_return(self, target_present):
        if not self.enabled or self.busy or self.pending or abs(self.net) < 1e-6:
            return False
        if target_present or time.time() - self.last_move < self.return_sec:
            return False
        net = self.net
        self._move(-1 if net > 0 else 1, abs(net))
        self.net = 0.0
        return True
