# -*- coding: utf-8 -*-
"""突发不适：把多种异常信号合并后再升级

摄像头不能诊断疾病。这里只判断“需要有人马上去看一眼”的行为组合：
  lying_off_rest  在床、沙发等休息区之外躺倒超过 ILL_LYING_SEC 秒
  motionless      倒卧或瘫坐后几乎没有动作超过 ILL_STILL_SEC 秒
  convulsion      倒卧时持续剧烈抖动（抽搐 / 挣扎）
  recent_fall     10 分钟内触发过跌倒
  distress_audio  30 秒内听到尖叫、呻吟、哭喊（需要声音模块）
  bathroom        在卫生间区域倒卧

两个及以上信号同时成立 → 急报；单独的长时间无动作（白天、非休息区）→ 值守提醒。
"""
import numpy as np


class IllnessMonitor:
    def __init__(self, lying_sec=60, still_sec=120, inactive_min=90, still_th=0.02,
                 shake_th=0.12, shake_sec=5):
        self.lying_sec, self.still_sec = lying_sec, still_sec
        self.inactive_min = inactive_min
        self.still_th, self.shake_th, self.shake_sec = still_th, shake_th, shake_sec
        self.st = {}

    @classmethod
    def from_env(cls, cfg):
        return cls(lying_sec=float(cfg('ILL_LYING_SEC', 60)),
                   still_sec=float(cfg('ILL_STILL_SEC', 120)),
                   inactive_min=float(cfg('STILL_ALERT_MIN', 90)))

    @staticmethod
    def _energy(prev, cur, bbox):
        """关键点平均位移 / 人体框对角线；无关键点时用框中心"""
        diag = max(np.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]), 1.0)
        if prev and cur:
            d = [np.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(prev, cur)
                 if a[2] > 0.3 and b[2] > 0.3]
            if d:
                return float(np.mean(d)) / diag
        return None

    def update(self, tid, kpts, bbox, posture, center, zones, now, in_poly,
               fell_recently=False, heard_distress=False, is_night=False):
        s = self.st.setdefault(tid, {'kpts': None, 'last_move': now, 'lie_since': None,
                                     'shake_since': None, 'last_center': center})
        e = self._energy(s['kpts'], kpts, bbox)
        if e is None:
            e = np.hypot(center[0] - s['last_center'][0], center[1] - s['last_center'][1]) \
                / max(np.hypot(bbox[2] - bbox[0], bbox[3] - bbox[1]), 1.0)
        s['kpts'], s['last_center'] = kpts, center
        if e > self.still_th:
            s['last_move'] = now
        lying = posture == 'lie'
        if lying:
            s['lie_since'] = s['lie_since'] or now
        else:
            s['lie_since'] = None
        if lying and e > self.shake_th:
            s['shake_since'] = s['shake_since'] or now
        else:
            s['shake_since'] = None

        on_rest = any(in_poly(center, z['poly']) for z in zones.get('rest_zones', []))
        in_bath = any(in_poly(center, z['poly']) for z in zones.get('bathroom_zones', []))
        still_for = now - s['last_move']

        reasons = []
        if lying and not on_rest and now - s['lie_since'] > self.lying_sec:
            reasons.append('在休息区外躺倒 %d 秒' % (now - s['lie_since']))
        if posture in ('lie', 'sit') and not on_rest and still_for > self.still_sec:
            reasons.append('%d 秒几乎没有动作' % still_for)
        if s['shake_since'] and now - s['shake_since'] > self.shake_sec:
            reasons.append('倒卧时持续抖动')
        if fell_recently:
            reasons.append('之前判断有跌倒')
        if heard_distress:
            reasons.append('听到呼喊或呻吟')
        if lying and in_bath:
            reasons.append('在卫生间倒卧')

        if len(reasons) >= 2:
            return ('CRITICAL', 'illness', '疑似突发不适：%s。' % '，'.join(reasons))
        if not is_night and not on_rest and still_for > self.inactive_min * 60:
            return ('GUARD', 'inactive', '已经 %d 分钟没有明显动作，请确认是否安好。'
                    % (still_for / 60))
        return None

    def forget(self, alive_tids):
        for tid in list(self.st):
            if tid not in alive_tids:
                self.st.pop(tid)
