# -*- coding: utf-8 -*-
"""警觉等级：决定这一刻要花多少算力

  idle   画面静止、没人（可能有睡着的宠物）。检测放慢到约 0.7 帧/秒，火焰每 5 秒查一次。
  watch  有动静或有人。检测 3 帧/秒，有人时跑姿态。
  alert  有未解除的值守 / 急报事件。检测 5 帧/秒，火焰每 2 秒查一次。

升级立即生效；降级要安静一段时间（hold 秒）才发生，避免来回抖动。

动静判断用的是极便宜的帧差：缩到 320 宽的灰度图，与背景做差。
整幅画面大面积同时变化（开灯、红外夜视切换）视为光照变化，不算动静，并重置背景。
"""
import time
import cv2
import numpy as np

IDLE, WATCH, ALERT = 'idle', 'watch', 'alert'
RANK = {IDLE: 0, WATCH: 1, ALERT: 2}


class MotionGate:
    def __init__(self, width=320, diff_th=25, min_ratio=0.002, light_ratio=0.5):
        self.width, self.diff_th = width, diff_th
        self.min_ratio, self.light_ratio = min_ratio, light_ratio
        self.bg = None
        self.last_motion = 0.0
        self.ratio = 0.0

    def update(self, frame, now):
        h, w = frame.shape[:2]
        g = cv2.cvtColor(cv2.resize(frame, (self.width, int(h * self.width / w))), cv2.COLOR_BGR2GRAY)
        g = cv2.GaussianBlur(g, (5, 5), 0).astype(np.float32)
        if self.bg is None:
            self.bg = g
            return 0.0
        diff = cv2.absdiff(g, self.bg)
        self.ratio = float((diff > self.diff_th).mean())
        if self.ratio > self.light_ratio:
            self.bg = g            # 光照突变：重置背景
            return 0.0
        cv2.accumulateWeighted(g, self.bg, 0.05)
        if self.ratio > self.min_ratio:
            self.last_motion = now
        return self.ratio


class AlertLevel:
    def __init__(self, hold=30.0):
        self.level = IDLE
        self.hold = hold
        self.since = time.time()
        self.time_in = {IDLE: 0.0, WATCH: 0.0, ALERT: 0.0}
        self._last = time.time()

    def update(self, now, motion_recent, persons, incident_open):
        self.time_in[self.level] += now - self._last
        self._last = now
        want = ALERT if incident_open else (WATCH if (motion_recent or persons) else IDLE)
        if RANK[want] > RANK[self.level]:
            self.level, self.since = want, now
        elif RANK[want] < RANK[self.level]:
            if now - self.since > self.hold:
                self.level, self.since = want, now
        else:
            self.since = now
        return self.level
