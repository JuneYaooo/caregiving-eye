# -*- coding: utf-8 -*-
"""活动记录：热力图 + 活跃时长 + 区域停留 + 按小时分布

宠物和人共用同一个类（kind='pet' / 'person'）。每天一份：
  events/activity_<kind>_YYYYMMDD.json   统计数据
  events/heatmap_<kind>_YYYYMMDD.npy     26×15 网格累计
  events/base_YYYYMMDD.jpg               当天底图（热力图叠加用）
程序重启后会读回当天已有数据继续累计。
"""
import copy, json, os, threading, time
import numpy as np

GRID = (15, 26)   # 行 × 列


class ActivityLog:
    def __init__(self, kind, out_dir, zone_keys=('pet_zones', 'cat_zones'),
                 move_th=0.008, unseen_hours=0):
        self.kind, self.out_dir = kind, out_dir
        self.zone_keys = zone_keys
        self.move_th = move_th            # 每次采样位移 / 画面宽 超过它算“活跃”
        self.unseen_hours = unseen_hours  # >0 时在久未出现时提醒
        self.day = None
        self.tracks = {}                  # tid -> {'c', 'ts', 'zones'}
        self._lock = threading.RLock()    # update 在主循环，save 在 io 线程
        self._reset(time.strftime('%Y%m%d'))

    def _paths(self, day):
        return (os.path.join(self.out_dir, 'activity_%s_%s.json' % (self.kind, day)),
                os.path.join(self.out_dir, 'heatmap_%s_%s.npy' % (self.kind, day)))

    def _reset(self, day):
        self.day = day
        self.stats = {'day': day, 'kind': self.kind, 'sightings': 0, 'active_sec': 0.0,
                      'still_sec': 0.0, 'distance': 0.0, 'hourly_active': [0.0] * 24,
                      'hourly_seen': [0.0] * 24, 'zones': {}, 'first_seen': None,
                      'last_seen': None, 'longest_still_sec': 0.0}
        self.grid = np.zeros(GRID, np.int32)
        jp, hp = self._paths(day)
        if os.path.exists(jp):
            try:
                self.stats.update(json.load(open(jp, encoding='utf-8')))
            except Exception:
                pass
        if os.path.exists(hp):
            try:
                self.grid = np.load(hp)
            except Exception:
                pass
        self.tracks = {}

    def update(self, tracks, zones, W, H, now, in_poly):
        with self._lock:
            self._update(tracks, zones, W, H, now, in_poly)

    def _update(self, tracks, zones, W, H, now, in_poly):
        day = time.strftime('%Y%m%d', time.localtime(now))
        if day != self.day:
            self.save()
            self._reset(day)
        hour = time.localtime(now).tm_hour
        for t in tracks:
            c = t.center()
            gx, gy = int(c[0] / W * GRID[1]), int(c[1] / H * GRID[0])
            if 0 <= gx < GRID[1] and 0 <= gy < GRID[0]:
                self.grid[gy, gx] += 1
            prev = self.tracks.get(t.tid)
            if prev is None:
                self.stats['sightings'] += 1
                prev = {'c': c, 'ts': now, 'zones': set(), 'still': 0.0}
                self.tracks[t.tid] = prev
            dt = min(now - prev['ts'], 5.0)   # 闲时检测间隔较长，上限放宽
            d = np.hypot(c[0] - prev['c'][0], c[1] - prev['c'][1]) / W
            if d > self.move_th:
                self.stats['active_sec'] += dt
                self.stats['hourly_active'][hour] += dt
                self.stats['distance'] += d
                prev['still'] = 0.0
            else:
                self.stats['still_sec'] += dt
                prev['still'] += dt
                self.stats['longest_still_sec'] = max(self.stats['longest_still_sec'], prev['still'])
            self.stats['hourly_seen'][hour] += dt
            # 区域停留
            cur = set()
            for key in self.zone_keys:
                for z in zones.get(key, []):
                    if in_poly(c, z['poly']):
                        cur.add(z['name'])
                        zs = self.stats['zones'].setdefault(z['name'], {'visits': 0, 'sec': 0.0})
                        zs['sec'] += dt
                        if z['name'] not in prev['zones']:
                            zs['visits'] += 1
            prev['zones'], prev['c'], prev['ts'] = cur, c, now
            ts = time.strftime('%H:%M', time.localtime(now))
            self.stats['first_seen'] = self.stats['first_seen'] or ts
            self.stats['last_seen'] = ts
            self.stats['last_seen_ts'] = now
        alive = {t.tid for t in tracks}
        for tid in list(self.tracks):
            if tid not in alive and now - self.tracks[tid]['ts'] > 30:
                self.tracks.pop(tid)

    def unseen_for(self, now):
        last = self.stats.get('last_seen_ts')
        return now - last if last else None

    def save(self):
        with self._lock:          # 先在锁内拍一份快照，写盘放到锁外
            jp, hp = self._paths(self.day)
            stats, grid = copy.deepcopy(self.stats), self.grid.copy()
        os.makedirs(self.out_dir, exist_ok=True)
        tmp = jp + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=1)
        os.replace(tmp, jp)
        np.save(hp, grid)


def summarize(stats, label):
    """把一天的统计变成几行中文"""
    if not stats or not stats.get('sightings'):
        return ['%s：今天没有记录到活动。' % label]
    act = stats['active_sec'] / 60
    seen = sum(stats['hourly_seen']) / 60
    lines = ['%s：出现 %d 次，画面中约 %.0f 分钟，活跃 %.0f 分钟，最早 %s，最晚 %s。'
             % (label, stats['sightings'], seen, act, stats['first_seen'], stats['last_seen'])]
    ha = stats['hourly_active']
    if max(ha) > 0:
        top = sorted(range(24), key=lambda h: -ha[h])[:3]
        lines.append('  最活跃时段：%s' % '、'.join('%02d:00' % h for h in sorted(top) if ha[h] > 0))
    if stats.get('zones'):
        zs = sorted(stats['zones'].items(), key=lambda kv: -kv[1]['sec'])
        lines.append('  区域停留：' + '，'.join('%s %d 次 / %.0f 分钟' % (k, v['visits'], v['sec'] / 60)
                                         for k, v in zs[:6]))
    if stats.get('longest_still_sec', 0) > 600:
        lines.append('  最长一次静止约 %.0f 分钟。' % (stats['longest_still_sec'] / 60))
    return lines
