# -*- coding: utf-8 -*-
"""谁算人：把“像人”的检测框筛成“家里的人”，全系统只认这一个口径

单帧检测只回答“这块像素像不像人”。石膏像、挂着的衣服、夜间红外噪点都会被当成人，
如果直接拿去算警觉等级、在家人数、活动统计，就会一直停在 watch 档、算错人数、污染作息基线。

  active   动过的人形（MobilityFilter 两次独立位移）或已合格的人。决定警觉等级、要不要跑姿态、入侵。
  people   合格的人：动过 + 关键点可信 + 不在家具格。决定在家人数、进出通报、活动统计、区域规则。

家具格子记忆：画面分 12×8 格，统计“没走过路的人形”占据每个格子的时间（6 小时指数衰减窗口）。
  某格及其相邻格被占据超过窗口的 50%（运行满 30 分钟后才生效）→ 家具格，格子里没走过路的人形不算人。
  - 只统计没走过路（累计位移不到画面宽 15%）的目标：真人走进来坐下，不会把自己的座位拉黑
  - 床、沙发等休息区（rest_zones）不统计
  - 衰减窗口：挪走雕像后，格子几小时内自动恢复
  持久化在 events/furniture_cells.json，重启后立即生效。

换编号继承：跟踪器偶尔丢失目标再找回时会换编号。原地出现的新编号（90 秒内、距离不到画面宽 8%）
  继承旧编号的资格，坐着不动的人不会因此被当成“走了”。
"""
import json, math, os

GRID_C, GRID_R = 12, 8


class PersonGate:
    def __init__(self, mobility, path=None, walk_th=0.15, tau=6 * 3600, warmup=1800, ratio=0.5,
                 inherit_sec=90, inherit_dist=0.08):
        self.mobility = mobility
        self.path = path
        self.walk_th, self.tau, self.warmup, self.ratio = walk_th, tau, warmup, ratio
        self.inherit_sec, self.inherit_dist = inherit_sec, inherit_dist
        self.qualified = set()
        self.walked = set()
        self.occ = {}            # 'col_row' -> 衰减后的占据秒数
        self.total = 0.0         # 衰减后的运行秒数
        self.lost = []           # [(ts, (cx, cy), walked)] 刚丢失的合格目标，用于换编号继承
        self.known = set()       # 见过的 tid
        self.last_pos = {}       # tid -> 最近位置（仅合格目标）
        self._last_ts = None
        self._last_save = 0.0
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            d = json.load(open(self.path, encoding='utf-8'))
            if d.get('version') == 2:
                self.occ = {k: float(v) for k, v in d.get('occ', {}).items()}
                self.total = float(d.get('total', 0))
        except Exception as e:
            print('[presence] 家具格子记忆读取失败，重新统计：', e)

    def save(self):
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
            tmp = self.path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'version': 2, 'total': round(self.total, 1),
                           'occ': {k: round(v, 1) for k, v in self.occ.items() if v > 1}}, f)
            os.replace(tmp, self.path)
        except Exception as e:
            print('[presence] 家具格子记忆保存失败：', e)

    # ---------- 家具格 ----------
    @staticmethod
    def cell(pt, W, H):
        return (min(int(pt[0] / max(W, 1) * GRID_C), GRID_C - 1),
                min(int(pt[1] / max(H, 1) * GRID_R), GRID_R - 1))

    def furniture(self, pt, W, H):
        """这个位置是不是家具格（相邻格一起算：静物的框中心抖动可能跨格）"""
        if self.total < self.warmup:
            return False
        c, r = self.cell(pt, W, H)
        near = sum(self.occ.get('%d_%d' % (c + dc, r + dr), 0.0)
                   for dc in (-1, 0, 1) for dr in (-1, 0, 1))
        return near > self.ratio * self.total

    def _accumulate(self, cells, dt):
        k = math.exp(-dt / self.tau)
        self.total = self.total * k + dt
        for key in list(self.occ):
            self.occ[key] *= k
        for key in cells:
            self.occ[key] = self.occ.get(key, 0.0) + dt

    # ---------- 每次检测后调用 ----------
    def update(self, persons, kpts_ok, W, H, ts, in_rest=lambda pt: False):
        """persons: 跟踪器输出的人形；kpts_ok: tid -> True / False / None（本轮没跑姿态，不改资格）
        返回 (active, people)"""
        dt = min(10.0, max(0.0, ts - self._last_ts)) if self._last_ts is not None else 0.0
        self._last_ts = ts
        alive = {t.tid for t in persons}

        # 丢失的合格目标记下位置，留给换编号继承
        for tid in list(self.last_pos):
            if tid not in alive:
                self.lost.append((ts, self.last_pos.pop(tid), tid in self.walked))
        self.lost = [x for x in self.lost if ts - x[0] < self.inherit_sec]

        cells = set()
        for t in persons:
            pt = t.center()
            if t.tid not in self.known:
                self.known.add(t.tid)
                self._inherit(t.tid, pt, W)
            self.mobility.update(t.tid, pt, W)
            if self.mobility.span(t.tid) > self.walk_th * W:
                self.walked.add(t.tid)
            if t.tid not in self.walked and not in_rest(pt):
                c, r = self.cell(pt, W, H)
                cells.add('%d_%d' % (c, r))
        if dt:
            self._accumulate(cells, dt)

        for t in persons:
            tid, pt = t.tid, t.center()
            stuck = tid not in self.walked and self.furniture(pt, W, H)
            if stuck:
                self.qualified.discard(tid)
            elif tid not in self.qualified and self.mobility.moved(tid) and kpts_ok.get(tid):
                self.qualified.add(tid)
            if tid in self.qualified:
                self.last_pos[tid] = pt

        self.qualified &= alive
        self.walked &= alive
        self.known &= alive
        if ts - self._last_save > 60:
            self._last_save = ts
            self.save()
        active = [t for t in persons if t.tid in self.qualified or self.mobility.moved(t.tid)]
        people = [t for t in persons if t.tid in self.qualified]
        return active, people

    def _inherit(self, tid, pt, W):
        best, bd = None, self.inherit_dist * W
        for i, (_, p, _) in enumerate(self.lost):
            d = math.hypot(pt[0] - p[0], pt[1] - p[1])
            if d < bd:
                best, bd = i, d
        if best is None:
            return
        _, _, walked = self.lost.pop(best)
        self.qualified.add(tid)
        if walked:
            self.walked.add(tid)
        self.mobility.mark_moved(tid, pt)
