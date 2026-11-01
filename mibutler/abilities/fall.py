# -*- coding: utf-8 -*-
"""跌倒：姿态状态机、家具化过滤、跌倒样本库

  PostureSM       关键点几何 + 迟滞，判断每个人站 / 坐 / 躺；站着 2.5 秒内变躺 → 'fall'，
                  躺着超过 30 秒 → 'lying_long'。挂在跟踪 id 上。
  MobilityFilter  从没自己移动过的“人”（雕像、摆件、夜间红外噪点）不参与跌倒、久卧、入侵、不适判断；
                  位移超过画面宽度 MOBILE_TH（默认 4%）一次就恢复。
                  代价：启动时已经在画面里、一直没动过的真人，在动之前不会触发久卧。
  FallSamples     每次报跌倒 / 疑似跌倒，把前后约 12 秒的关键点存成样本（只有坐标，没有画面）；
                  主人回复“没事”标为误报、回复“属实”标为真跌倒，之后可以加进 ST-GCN 训练
                  （scripts/fall_gcn/train.py --samples events/samples）。

骨架时序分类器和并联研判在 fall_gcn.py。
"""
import glob, json, os, time
from collections import deque
import numpy as np

FALL_CFG = {
    'stand_aspect_max': 0.9, 'lying_aspect_min': 1.1,
    'fall_transition_secs': 2.5, 'min_score': 0.4,
    'lying_min_secs': 30, 'lying_realert': 300,
    'require_kpts': True,  # 跌倒/久卧告警必须关键点确认(雕像/摆件提不出可信肩髋点)
}


class PostureSM:
    """姿态状态机: 关键点几何 + 迟滞; 挂在 track_id 上"""
    HYS = 2  # 新状态需连续2次判定才切换

    def __init__(self):
        self.sm = {}

    def _geom(self, kpts, bbox):
        """返回 (angle_deg, torso_len, aspect)"""
        x1, y1, x2, y2 = bbox
        aspect = (x2 - x1) / max(y2 - y1, 1e-6)
        if not kpts:
            return None, None, aspect
        def mid(i, j):
            a, b = kpts[i], kpts[j]
            if a[2] > 0.3 and b[2] > 0.3:
                return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            return None
        ms = mid(5, 6)   # 双肩中点
        mh = mid(11, 12)  # 双髋中点
        if not ms or not mh:
            return None, None, aspect
        vx, vy = mh[0] - ms[0], mh[1] - ms[1]
        angle = abs(np.degrees(np.arctan2(vx, vy)))  # 0=竖直, 90=水平
        return angle, np.hypot(vx, vy), aspect

    def _state_of(self, angle, aspect):
        if angle is None:
            return 'lie' if aspect > FALL_CFG['lying_aspect_min'] else \
                   ('stand' if aspect < FALL_CFG['stand_aspect_max'] else 'unknown')
        if angle < 35:
            return 'stand'
        if angle > 65:
            return 'lie'
        return 'sit'

    def update(self, tid, kpts, bbox, now):
        st = self.sm.setdefault(tid, {'state': 'unknown', 'cand': None, 'cand_n': 0,
                                      'last_stand_ts': now, 'lying_since': None,
                                      'last_lying_alert': 0, 'fall_alerted': False,
                                      'kpts_ok': False})
        if st['state'] == 'stand':
            st['last_stand_ts'] = now   # 持续站立时也要刷新，否则站久了的人跌倒永远判不出来
        angle, tlen, aspect = self._geom(kpts, bbox)
        st['kpts_ok'] = angle is not None   # 关键点可信(肩髋都>0.3)才算"确认是人"
        new = self._state_of(angle, aspect)
        if new == st['state'] or new == 'unknown':
            st['cand'], st['cand_n'] = None, 0
        else:
            st['cand_n'] = st['cand_n'] + 1 if st['cand'] == new else 1
            st['cand'] = new
            if st['cand_n'] >= self.HYS:
                prev, st['state'] = st['state'], new
                st['cand'], st['cand_n'] = None, 0
                if new == 'stand':
                    st['last_stand_ts'] = now
                    st['fall_alerted'] = False
                if new == 'lie':
                    st['lying_since'] = now
                    # 跌倒判定: 近期站立 → 突然倒地 (必须关键点确认)
                    if prev == 'stand' and now - st['last_stand_ts'] < FALL_CFG['fall_transition_secs'] \
                            and not st['fall_alerted'] \
                            and (not FALL_CFG.get('require_kpts') or st['kpts_ok']):
                        st['fall_alerted'] = True
                        return 'fall'
                else:
                    st['lying_since'] = None
        # 倒地久卧 (必须关键点确认; 纯框比例的"躺"可能是雕像/包/杂物)
        if st['state'] == 'lie' and st['lying_since'] \
                and now - st['lying_since'] > FALL_CFG['lying_min_secs'] \
                and now - st['last_lying_alert'] > FALL_CFG['lying_realert'] \
                and (not FALL_CFG.get('require_kpts') or st['kpts_ok']):
            st['last_lying_alert'] = now
            return 'lying_long'
        return None

    def state(self, tid):
        return self.sm[tid]['state'] if tid in self.sm else 'unknown'

    def forget(self, alive_tids):
        for tid in list(self.sm):
            if tid not in alive_tids:
                self.sm.pop(tid)


class MobilityFilter:
    """家具化过滤：跟踪目标的锚点位移超过 th × 画面宽才算“动过”，动过一次就一直有告警资格。
    防单次框抖动绕过：需要累计两次独立位移事件才认定“动过”（真人走动每帧都在跳，两次瞬间就有）。"""

    def __init__(self, th=0.04, jumps_needed=2):
        self.th = th
        self.jumps_needed = jumps_needed
        self.seen = {}      # tid -> {'ax', 'ay', 'jumps'}

    @classmethod
    def from_env(cls, cfg):
        return cls(float(cfg('MOBILE_TH', 0.04)))

    def update(self, tid, center, W):
        cx, cy = center
        m = self.seen.setdefault(tid, {'ax': cx, 'ay': cy, 'jumps': 0})
        if abs(cx - m['ax']) + abs(cy - m['ay']) > self.th * W:
            m['ax'], m['ay'] = cx, cy
            m['jumps'] += 1
        return m['jumps'] >= self.jumps_needed

    def moved(self, tid):
        return self.seen.get(tid, {}).get('jumps', 0) >= self.jumps_needed

    def forget(self, alive_tids):
        for tid in list(self.seen):
            if tid not in alive_tids:
                self.seen.pop(tid)


class FallSamples:
    """跌倒样本库：events/samples/fall_<时间>_<事件id>.npz（ts, kpts）+ 同名 .json（标签）

    标签：null 未确认 / 0 误报（主人回复“没事”）/ 1 属实（主人回复“属实”）。
    事件时刻 t_event 是相对样本第一帧的秒数，训练时按“着地时刻”处理。"""

    def __init__(self, out_dir, before=8.0, after=4.0):
        self.dir = out_dir
        self.before, self.after = before, after
        self.hist = {}       # tid -> deque[(ts, kpts or None)]
        self.pending = []    # [{'tid','t','incident','type','due'}]
        self.early = {}      # 样本还没写盘就收到的标签 incident -> label

    def push(self, tid, ts, kpts):
        h = self.hist.setdefault(tid, deque())
        h.append((ts, np.asarray(kpts, np.float32) if kpts else None))
        while h and ts - h[0][0] > self.before + self.after + 2:
            h.popleft()

    def mark(self, tid, t_event, incident_id, etype):
        if incident_id and not any(p['incident'] == incident_id for p in self.pending):
            self.pending.append({'tid': tid, 't': t_event, 'incident': incident_id,
                                 'type': etype, 'due': t_event + self.after})

    def flush(self, now, force=False):
        """事件后 after 秒（或目标已丢失、程序退出）把样本写盘；返回写出的文件"""
        out = []
        for p in list(self.pending):
            if now < p['due'] and not force:
                continue
            self.pending.remove(p)
            rows = [(t, k) for t, k in self.hist.get(p['tid'], ())
                    if p['t'] - self.before <= t <= p['t'] + self.after]
            if len(rows) < 5:
                continue
            t0 = rows[0][0]
            ts = np.array([t - t0 for t, _ in rows], np.float64)
            kp = np.stack([k if k is not None else np.zeros((17, 3), np.float32) for _, k in rows])
            os.makedirs(self.dir, exist_ok=True)
            base = os.path.join(self.dir, 'fall_%s_%s' % (
                time.strftime('%Y%m%d_%H%M%S', time.localtime(p['t'])), p['incident']))
            np.savez_compressed(base + '.npz', ts=ts, kpts=kp)
            with open(base + '.json', 'w', encoding='utf-8') as f:
                json.dump({'incident': p['incident'], 'type': p['type'], 't_event': p['t'] - t0,
                           'label': self.early.pop(p['incident'], None), 'time': time.strftime('%Y-%m-%d %H:%M:%S',
                                                                time.localtime(p['t']))},
                          f, ensure_ascii=False, indent=1)
            out.append(base)
        return out

    def label(self, incident_id, label):
        """主人确认后给样本打标签；样本还没写盘时先记下，写盘后再补"""
        if any(p['incident'] == incident_id for p in self.pending):
            self.early[incident_id] = label
            return 1
        n = 0
        for path in glob.glob(os.path.join(self.dir, 'fall_*_%s.json' % incident_id)):
            try:
                meta = json.load(open(path, encoding='utf-8'))
                meta['label'] = label
                with open(path, 'w', encoding='utf-8') as f:
                    json.dump(meta, f, ensure_ascii=False, indent=1)
                n += 1
            except Exception as e:
                print('[samples] 标注失败：', e)
        return n

    def forget(self, alive_tids):
        waiting = {p['tid'] for p in self.pending}
        for tid in list(self.hist):
            if tid not in alive_tids and tid not in waiting:
                self.hist.pop(tid)
