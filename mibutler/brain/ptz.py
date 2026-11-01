# -*- coding: utf-8 -*-
"""云台是共享资源：谁能转、转完怎么回来、转动期间哪些规则要暂停

  - 同一时间只允许一个转动指令（加锁），指令在 io 线程执行，不阻塞主循环。
  - 记录累计水平转动量；目标离开 return_sec 秒后，反向转回原位（近似复位，长期会有漂移，
    建议定期在米家 App 里校正一次）。
  - 转动期间及复位后 settle_sec 秒内，zones_valid=False：区域规则、热力图暂停，
    因为画面坐标已经不对应原来标定的区域。
  - ViewCheck 定期拿当前画面和标定时的参考画面比，发现复位漂移或有人手动转过云台时，
    同样暂停区域规则并提醒，转回来后自动恢复。
"""
import os, threading, time
import numpy as np
import cv2


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
        from mibutler.camera.onvif import continuous_move, stop, get_profile_token
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


class ViewCheck:
    """画面对齐检查：和标定区域时的参考画面比，判断镜头是不是被转过

    云台跟踪后的复位是“反向转同样时长”，长期会有漂移；有人在米家 App 里手动转了云台，
    执事也不知道。这里每隔一段时间（画面安静时）用相位相关算当前画面相对参考画面的平移：
      连续 need 次偏移超过画面的 shift_th → 判为偏移，区域规则和热力图暂停并提醒
      连续 need 次回到阈值内            → 恢复
    比的是边缘图，开关灯、白天黑夜切换影响较小；相关度太低（画面变化太大、全黑）时本次不下结论。

    参考画面：zones.json 旁边的 zones_ref.jpg。没有时（或 zones.json 比它新，说明重新标定过）
    用当前画面自动生成，所以重新标定区域时请让云台处在标定位置。"""

    W = 320

    def __init__(self, ref_path, zones_path=None, shift_th=0.04, need=3, min_resp=0.08):
        self.ref_path, self.zones_path = ref_path, zones_path
        self.shift_th, self.need, self.min_resp = shift_th, need, min_resp
        self.ref = None
        self.aligned = True
        self.bad = self.good = 0
        self.last = None             # (dx_frac, dy_frac, resp)
        if os.path.exists(ref_path) and not self._zones_newer():
            img = cv2.imread(ref_path)
            if img is not None:
                self.ref = self._feat(img)

    def _zones_newer(self):
        return bool(self.zones_path and os.path.exists(self.zones_path)
                    and os.path.getmtime(self.zones_path) > os.path.getmtime(self.ref_path))

    def _feat(self, frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        h = int(g.shape[0] * self.W / g.shape[1])
        g = cv2.GaussianBlur(cv2.resize(g, (self.W, h), interpolation=cv2.INTER_AREA), (3, 3), 0)
        gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
        m = cv2.magnitude(gx, gy)
        return m / (m.mean() + 1e-6)

    def check(self, frame):
        """返回 'shifted' / 'restored' / None（状态没变）"""
        cur = self._feat(frame)
        if self.ref is None or self.ref.shape != cur.shape:
            self.ref = cur
            cv2.imwrite(self.ref_path, frame)
            return None
        win = cv2.createHanningWindow(cur.shape[::-1], cv2.CV_32F)
        (dx, dy), resp = cv2.phaseCorrelate(self.ref, cur, win)
        fx, fy = dx / cur.shape[1], dy / cur.shape[0]
        self.last = (round(fx, 3), round(fy, 3), round(resp, 3))
        if resp < self.min_resp:
            return None
        if np.hypot(fx, fy) > self.shift_th:
            self.bad, self.good = self.bad + 1, 0
            if self.aligned and self.bad >= self.need:
                self.aligned = False
                return 'shifted'
        else:
            self.good, self.bad = self.good + 1, 0
            if not self.aligned and self.good >= self.need:
                self.aligned = True
                return 'restored'
        return None
