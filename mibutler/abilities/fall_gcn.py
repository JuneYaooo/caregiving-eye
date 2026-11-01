# -*- coding: utf-8 -*-
"""骨架时序跌倒分类（ST-GCN）+ 与姿态规则的并联研判

姿态规则只看“前一刻站着、这一刻躺着”，容易把弯腰、坐下、躺上沙发误判成跌倒，
也容易漏掉缓慢滑倒。骨架分类器看的是最近几秒 17 个关键点的整段动作，学的是跌倒特有的
失衡、下坠、着地过程。

  输入   每个人最近 WINDOW_SEC 秒的 YOLOv8-pose 关键点，按时间插值成 T 帧，
         以人身高归一化后得到 (1, 3, T, 17)：x、y、置信度
  输出   跌倒概率（ONNX，CPU 上一次不到 1 毫秒）
  模型   scripts/fall_gcn/train.py 用 UR Fall 等公开数据集训练并导出，放到 FALL_GCN_MODEL

并联研判（FALL_FUSION=both，默认）：
  规则报跌倒，且前后几秒内模型也判跌倒      → 急报“判断有人跌倒”
  规则报跌倒，模型没确认                    → 值守“疑似跌倒”（仍抓拍录像；若一直没起身，
                                              倒地久卧会在 30 秒后升级为急报）
  规则没报，模型高置信度判跌倒且人已躺/坐    → 值守“疑似跌倒”
FALL_FUSION=either：规则单独报也直接急报，模型只负责补报规则漏掉的。
没有模型文件时本模块不启用，跌倒判断与原来完全一样。

这里的窗口采样与归一化函数也被训练脚本直接引用，保证训练和运行时处理方式一致。
"""
import os
from collections import deque
import numpy as np

WINDOW_SEC = 4.0      # 看最近几秒
T = 20                # 插值成多少帧（4 秒 20 帧 = 5 fps，与执事有人时的检测频率相当）
V = 17                # COCO 关键点数
KP_TH = 0.3           # 关键点置信度阈值
MAX_GAP = 1.0         # 两次采样相隔超过这么久，中间不插值

# COCO-17 骨架连线（父 → 子，以躯干为根）
EDGES = [(5, 6), (5, 11), (6, 12), (11, 12),                 # 躯干
         (5, 7), (7, 9), (6, 8), (8, 10),                     # 手臂
         (11, 13), (13, 15), (12, 14), (14, 16),              # 腿
         (5, 0), (6, 0), (0, 1), (0, 2), (1, 3), (2, 4)]      # 头
FLIP = [0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15]   # 左右镜像对应


def resample(samples, t_end, window=WINDOW_SEC, frames=T):
    """samples: [(ts, kpts(17,3) 或 None)]，按时间升序。
    返回 (frames, 17, 3) 数组与有效帧比例。缺帧处置信度为 0。"""
    out = np.zeros((frames, V, 3), np.float32)
    if not samples:
        return out, 0.0
    ts = np.array([s[0] for s in samples], np.float64)
    grid = t_end - window + np.arange(frames) * window / (frames - 1)
    good = 0
    for j, t in enumerate(grid):
        i = int(np.searchsorted(ts, t, side='right')) - 1      # ts[i] <= t < ts[i+1]
        a = samples[i][1] if i >= 0 else None
        b = samples[i + 1][1] if i + 1 < len(samples) else None
        ta = ts[i] if i >= 0 else -1e18
        tb = ts[i + 1] if i + 1 < len(samples) else 1e18
        if a is not None and b is not None and tb - ta <= MAX_GAP:
            w = (t - ta) / max(tb - ta, 1e-6)
            fr = np.where((a[:, 2:3] > KP_TH) & (b[:, 2:3] > KP_TH),
                          a * (1 - w) + b * w,
                          np.where(a[:, 2:3] >= b[:, 2:3], a, b))
        elif a is not None and t - ta <= MAX_GAP / 2:
            fr = a
        elif b is not None and tb - t <= MAX_GAP / 2:
            fr = b
        else:
            continue
        out[j] = fr
        if (fr[:, 2] > KP_TH).sum() >= 5:
            good += 1
    return out, good / frames


def normalize(win):
    """(T,17,3) 像素坐标 → 以人为尺度的相对坐标（返回 (3,T,17)）

    原点取窗口里第一帧有效的髋部中心（没有髋就用所有可见点的中心），
    尺度取窗口内人体最大外接尺寸（通常是站立时的身高），这样下坠、倒地的位移保留下来，
    又和人离摄像头远近无关。"""
    x = win.copy()
    vis = x[..., 2] > KP_TH
    origin, scale = None, 0.0
    for t in range(x.shape[0]):
        v = vis[t]
        if v.sum() < 3:
            continue
        pts = x[t, v, :2]
        scale = max(scale, float(np.ptp(pts[:, 0])), float(np.ptp(pts[:, 1])))
        if origin is None:
            origin = x[t, [11, 12], :2].mean(0) if vis[t, 11] and vis[t, 12] else pts.mean(0)
    if origin is None or scale < 1e-3:
        return np.zeros((3,) + x.shape[:2], np.float32)
    x[..., :2] = (x[..., :2] - origin) / scale
    x[~vis] = 0
    return x.transpose(2, 0, 1).astype(np.float32)


def softmax(z):
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


class FallClassifier:
    """按人（跟踪 id）缓存关键点序列，每次更新算一次跌倒概率"""

    def __init__(self, path, threads=1, min_cover=0.6):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(path, so, ['CPUExecutionProvider'])
        self.input = self.sess.get_inputs()[0].name
        self.min_cover = min_cover
        self.buf = {}           # tid -> deque[(ts, kpts)]
        self.probs = {}         # tid -> deque[(ts, p)]

    @classmethod
    def from_env(cls, cfg):
        path = cfg('FALL_GCN_MODEL', os.path.join(cfg('MODELS_DIR', 'models'), 'fall_stgcn.onnx'))
        if not path or not os.path.exists(path):
            return None
        try:
            return cls(path)
        except Exception as e:
            print('[fall_gcn] 模型加载失败，跳过：', e)
            return None

    def predict(self, x):
        """x: (3,T,17) 或 (N,3,T,17) → 跌倒概率"""
        if x.ndim == 3:
            x = x[None]
        logits = self.sess.run(None, {self.input: x.astype(np.float32)})[0]
        return np.array([softmax(l)[1] for l in logits])

    def update(self, tid, ts, kpts):
        """kpts: 17×[x,y,conf] 或 None；返回本次概率（数据不够时返回 None）"""
        b = self.buf.setdefault(tid, deque())
        b.append((ts, np.asarray(kpts, np.float32) if kpts else None))
        while b and ts - b[0][0] > WINDOW_SEC + MAX_GAP:
            b.popleft()
        win, cover = resample(list(b), ts)
        if cover < self.min_cover:
            return None
        p = float(self.predict(normalize(win))[0])
        h = self.probs.setdefault(tid, deque(maxlen=64))
        h.append((ts, p))
        return p

    def max_prob(self, tid, since):
        return max((p for t, p in self.probs.get(tid, ()) if t >= since), default=0.0)

    def forget(self, alive_tids):
        for d in (self.buf, self.probs):
            for tid in list(d):
                if tid not in alive_tids:
                    d.pop(tid)


class FallFusion:
    """把规则和模型的判断合成一个结论。step() 返回 [(tid, kind, prob)]，
    kind: 'confirmed'（两者都报）| 'rule_only' | 'model_only' | 'rule'（either 模式下规则单独报）"""

    def __init__(self, clf, mode='both', threshold=0.6, strong=0.9, wait=2.5, lookback=3.0):
        self.clf, self.mode = clf, mode
        self.th, self.strong = threshold, strong
        self.wait, self.lookback = wait, lookback
        self.pending = {}        # tid -> 规则报跌倒的时间
        self.last_report = {}    # tid -> 最近一次上报时间

    @classmethod
    def from_env(cls, clf, cfg):
        return cls(clf, mode=cfg('FALL_FUSION', 'both').strip().lower(),
                   threshold=float(cfg('FALL_GCN_TH', 0.6)),
                   strong=float(cfg('FALL_GCN_STRONG', 0.9)))

    def rule_fired(self, tid, now):
        if self.mode == 'either':
            self.last_report[tid] = now
            return [(tid, 'rule', self.clf.max_prob(tid, now - self.lookback))]
        self.pending[tid] = now
        return []

    def step(self, now, probs, postures):
        """probs: {tid: 本轮概率或 None}；postures: {tid: 'stand'|'sit'|'lie'|...}"""
        out = []
        for tid, t0 in list(self.pending.items()):
            p = self.clf.max_prob(tid, t0 - self.lookback)
            if p >= self.th:
                out.append((tid, 'confirmed', p))
            elif now - t0 > self.wait:
                out.append((tid, 'rule_only', p))
            else:
                continue
            self.pending.pop(tid)
            self.last_report[tid] = now
        for tid, p in probs.items():
            if p is None or p < self.strong or tid in self.pending:
                continue
            if postures.get(tid) not in ('lie', 'sit'):
                continue
            if now - self.last_report.get(tid, 0) < 30:
                continue
            self.last_report[tid] = now
            out.append((tid, 'model_only', p))
        return out

    def forget(self, alive_tids):
        for tid in list(self.last_report):
            if tid not in alive_tids and tid not in self.pending:
                self.last_report.pop(tid)
