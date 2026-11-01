# -*- coding: utf-8 -*-
"""儿童危险：儿童判定 + 危险区域 + 无成人陪同

儿童判定（启发式，需按机位校准，可能误判）：
  1. 身体比例：腿长（髋→踝）/ 身长（鼻→踝）。成人约 0.5 以上，幼儿明显更小。
  2. 身高带（可选）：站立时身长 / 画面高度落在 CHILD_HEIGHT_BAND 内。只在固定机位、
     活动区域离镜头距离差不多时可靠；云台转动后需重新标定。
  每个 track 累计投票，票数够了才定性，避免单帧抖动。

告警：
  - 儿童进入 danger_zones（灶台、窗台、阳台、大门等）且附近 ADULT_NEAR 范围内没有成人 → 急报
  - 画面里只有儿童、没有成人超过 CHILD_ALONE_MIN 分钟 → 值守提醒
"""
import numpy as np


def _pt(k, i, th=0.3):
    return (k[i][0], k[i][1]) if k and k[i][2] > th else None


def _mid(a, b):
    if a and b:
        return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return a or b


class ChildSafety:
    def __init__(self, height_band=None, leg_ratio_max=0.46, adult_near=0.25,
                 danger_sec=3.0, alone_min=10.0, min_votes=5):
        self.height_band = height_band
        self.leg_ratio_max = leg_ratio_max
        self.adult_near = adult_near
        self.danger_sec = danger_sec
        self.alone_min = alone_min
        self.min_votes = min_votes
        self.votes = {}          # tid -> [child_votes, total]
        self.in_danger = {}      # (tid, zone) -> since
        self.alone_since = None

    @classmethod
    def from_env(cls, cfg):
        if cfg('HAS_CHILD', '0') not in ('1', 'true', 'yes'):
            return None
        band = None
        txt = cfg('CHILD_HEIGHT_BAND', '')
        if '-' in txt:
            a, b = txt.split('-')
            band = (float(a), float(b))
        return cls(height_band=band,
                   leg_ratio_max=float(cfg('CHILD_LEG_RATIO', 0.46)),
                   adult_near=float(cfg('ADULT_NEAR', 0.25)),
                   alone_min=float(cfg('CHILD_ALONE_MIN', 10)))

    def _vote(self, kpts, H, standing):
        """返回 True=像儿童, False=像成人, None=无法判断"""
        if not kpts or not standing:
            return None
        nose = _pt(kpts, 0)
        hip = _mid(_pt(kpts, 11), _pt(kpts, 12))
        ankle = _mid(_pt(kpts, 15), _pt(kpts, 16))
        if not (nose and hip and ankle):
            return None
        body = np.hypot(ankle[0] - nose[0], ankle[1] - nose[1])
        leg = np.hypot(ankle[0] - hip[0], ankle[1] - hip[1])
        if body < 1:
            return None
        votes = [leg / body < self.leg_ratio_max]
        if self.height_band:
            r = body / H
            votes.append(self.height_band[0] <= r <= self.height_band[1])
        return sum(votes) * 2 > len(votes)

    def is_child(self, tid):
        c, n = self.votes.get(tid, (0, 0))
        return n >= self.min_votes and c / n > 0.6

    def update(self, persons, zones, W, H, now, in_poly):
        """persons: [(track, kpts, posture)]；返回事件列表 [(level, type, text)]"""
        events = []
        for t, kpts, posture in persons:
            v = self._vote(kpts, H, posture == 'stand')
            if v is not None:
                c, n = self.votes.get(t.tid, (0, 0))
                self.votes[t.tid] = (c + int(v), n + 1)
        kids = [t for t, _, _ in persons if self.is_child(t.tid)]
        adults = [t for t, _, _ in persons if not self.is_child(t.tid)]

        def foot(t):
            x1, y1, x2, y2 = t.bbox
            return ((x1 + x2) / 2, y2)

        live = set()
        for k in kids:
            fx, fy = foot(k)
            near_adult = any(np.hypot(foot(a)[0] - fx, foot(a)[1] - fy) < self.adult_near * W
                             for a in adults)
            for z in zones.get('danger_zones', []):
                inside = in_poly((fx, fy), z['poly']) or in_poly(k.center(), z['poly'])
                key = (k.tid, z['name'])
                if not inside:
                    continue
                live.add(key)
                since = self.in_danger.setdefault(key, now)
                if now - since >= self.danger_sec:
                    if near_adult:
                        events.append(('GUARD', 'child_danger_watched',
                                       '孩子在%s附近，有大人在旁边，请留意。' % z['name']))
                    else:
                        events.append(('CRITICAL', 'child_danger',
                                       '孩子独自靠近%s，附近没有大人！已存证。' % z['name']))
        for key in list(self.in_danger):
            if key not in live:
                self.in_danger.pop(key)

        if kids and not adults:
            self.alone_since = self.alone_since or now
            if now - self.alone_since > self.alone_min * 60:
                events.append(('GUARD', 'child_alone',
                               '画面里只有孩子，已 %d 分钟没看到大人。'
                               % ((now - self.alone_since) / 60)))
        else:
            self.alone_since = None
        return events

    def forget(self, alive_tids):
        """跟踪器已经丢弃的目标不再保留投票，避免长期运行内存一直涨"""
        for tid in list(self.votes):
            if tid not in alive_tids:
                self.votes.pop(tid)
