# -*- coding: utf-8 -*-
"""画面变化比对：两张图比一比、长时间段粗扫、在区间里二分定位到秒。

比较的是“纹理 / 边缘”而不是亮度：先做局部直方图均衡（CLAHE），再算梯度图，按 16×16 小块做
归一化相关。日照移动、开关灯大多只改亮度，不改边缘，因此不算变化；超过 60% 的块同时变化，
判为光照或镜头切换（红外夜视、云台转动），不当成“东西被动了”。

用法:
  compare  A.jpg B.jpg [--roi x1,y1,x2,y2] [--out diff.jpg]
           两张图哪里变了。roi 用 0~1 的相对坐标，只看这块区域。
  scan     --video rec.mp4 | --images 'events/snap_*.jpg'  [--start S --end S] [--step 600] [--roi ...]
           粗扫：每隔 step 秒取一帧和上一帧比，列出发生持续变化的时间区间（可能有多段）。
  bisect   --video rec.mp4 | --images ...  --start S --end S [--roi ...]
           细化：已知区间首尾不同，二分找出变化发生的时刻（约 log2(秒数) 次比较）。
           每个探测点连看 3 帧取多数，避免“正好有人走过挡住”被当成变化。

图片序列的时间：从文件名里的 YYYYMMDD_HHMMSS 解析；解析不到就按顺序编号。
视频的时间：秒（相对视频开头）；--clock HH:MM:SS 可把输出换算成墙上时间。
"""
import argparse, glob, json, os, re, sys
import cv2
import numpy as np

BLOCK = 16
WIDTH = 320


# ---------------- 比较 ----------------

def _prep(img):
    h, w = img.shape[:2]
    g = cv2.cvtColor(cv2.resize(img, (WIDTH, int(h * WIDTH / w))), cv2.COLOR_BGR2GRAY)
    g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
    g = cv2.GaussianBlur(g, (5, 5), 0).astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1)
    return cv2.magnitude(gx, gy)


def _roi_mask(shape, roi):
    rows, cols = shape
    m = np.zeros(shape, bool)
    if not roi:
        m[:] = True
        return m
    x1, y1, x2, y2 = roi
    m[int(y1 * rows):max(int(np.ceil(y2 * rows)), int(y1 * rows) + 1),
      int(x1 * cols):max(int(np.ceil(x2 * cols)), int(x1 * cols) + 1)] = True
    return m


def compare(a, b, roi=None, ncc_th=0.5, min_energy=8.0):
    """返回 {'score': ROI 内变化块占比, 'global': 全图变化块占比, 'lighting': 是否像光照/镜头切换,
             'changed': bool, 'boxes': [[x1,y1,x2,y2] 相对坐标]}"""
    ga, gb = _prep(a), _prep(b)
    if ga.shape != gb.shape:
        gb = cv2.resize(gb, (ga.shape[1], ga.shape[0]))
    rows, cols = ga.shape[0] // BLOCK, ga.shape[1] // BLOCK
    blk = lambda g: g[:rows * BLOCK, :cols * BLOCK].reshape(rows, BLOCK, cols, BLOCK).mean(axis=(1, 3))
    # 噪声底线：最平坦的那 20% 块的能量 ×3。纯色桌面、白墙上的噪点被 CLAHE 放大后也有“纹理”，不能算
    floor = max(min_energy, 3 * float(np.percentile(np.minimum(blk(ga), blk(gb)), 20)))
    changed = np.zeros((rows, cols), bool)
    for r in range(rows):
        for c in range(cols):
            pa = ga[r * BLOCK:(r + 1) * BLOCK, c * BLOCK:(c + 1) * BLOCK].ravel()
            pb = gb[r * BLOCK:(r + 1) * BLOCK, c * BLOCK:(c + 1) * BLOCK].ravel()
            ea, eb = pa.mean(), pb.mean()
            if max(ea, eb) < floor:               # 两边都是平坦区（白墙、纯色桌面），没信息
                continue
            if min(ea, eb) < 0.35 * max(ea, eb):  # 一边有纹理一边没有：多了 / 少了东西
                changed[r, c] = True
                continue
            pa, pb = pa - ea, pb - eb
            ncc = float(pa @ pb / (np.linalg.norm(pa) * np.linalg.norm(pb) + 1e-6))
            changed[r, c] = ncc < ncc_th
    # 孤立的单块多半是噪点：相连（含斜向）的变化块至少 2 个才保留
    n, lab, stats, _ = cv2.connectedComponentsWithStats(changed.astype(np.uint8), connectivity=8)
    keep = [i for i in range(1, n) if stats[i][4] >= 2]
    k = np.isin(lab, keep)
    mask = _roi_mask((rows, cols), roi)
    g_ratio = float(changed.mean())
    score = float(k[mask].mean()) if mask.any() else 0.0
    boxes = []
    for i in keep:
        x, y, w, h, _ = stats[i]
        if mask[y:y + h, x:x + w].any():
            boxes.append([round(float(v), 3) for v in (x / cols, y / rows, (x + w) / cols, (y + h) / rows)])
    lighting = g_ratio > 0.6
    return {'score': round(score, 3), 'global': round(g_ratio, 3), 'lighting': lighting,
            'changed': (not lighting) and score > 0.03 and bool(boxes), 'boxes': boxes}


def draw(img, boxes, out):
    h, w = img.shape[:2]
    vis = img.copy()
    for x1, y1, x2, y2 in boxes:
        cv2.rectangle(vis, (int(x1 * w), int(y1 * h)), (int(x2 * w), int(y2 * h)), (0, 0, 255), 3)
    cv2.imwrite(out, vis)
    return out


# ---------------- 取帧 ----------------

class Source:
    """统一的按时间取帧：视频按秒 seek，图片序列按文件名时间取最近的一张"""

    def __init__(self, video=None, images=None):
        self.video = video
        if video:
            self.cap = cv2.VideoCapture(video)
            if not self.cap.isOpened():
                sys.exit('打不开视频 %s' % video)
            fps = self.cap.get(cv2.CAP_PROP_FPS) or 0
            n = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
            self.duration = n / fps if fps else 0
            self.times = None
        else:
            files = sorted(glob.glob(images))
            if not files:
                sys.exit('没有匹配的图片：%s' % images)
            ts = [_file_time(f) for f in files]
            if any(t is None for t in ts):
                ts = list(range(len(files)))
            order = sorted(range(len(files)), key=lambda i: ts[i])
            self.files = [files[i] for i in order]
            self.times = [ts[i] for i in order]
            self.duration = self.times[-1]

    def start(self):
        return 0.0 if self.video else self.times[0]

    def at(self, t):
        if self.video:
            self.cap.set(cv2.CAP_PROP_POS_MSEC, max(t, 0) * 1000)
            ok, f = self.cap.read()
            return f if ok else None
        i = int(np.argmin([abs(x - t) for x in self.times]))
        return cv2.imread(self.files[i])

    def name(self, t):
        if self.video:
            return None
        i = int(np.argmin([abs(x - t) for x in self.times]))
        return os.path.basename(self.files[i])


def _file_time(path):
    m = re.search(r'(\d{8})_(\d{6})', os.path.basename(path))
    if not m:
        return None
    d, t = m.groups()
    return int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:])   # 当天秒数


def fmt(t, clock=None):
    if clock is not None:
        t = t + clock
    t = int(round(t))
    return '%02d:%02d:%02d' % (t // 3600 % 24, t // 60 % 60, t % 60)


# ---------------- 粗扫 / 二分 ----------------

def scan(src, start, end, step, roi):
    """相邻采样点两两比较，返回发生变化的区间；光照切换单独标出"""
    out, prev_t, prev = [], start, src.at(start)
    t = start + step
    while t <= end + 1e-6:
        cur = src.at(t)
        if prev is not None and cur is not None:
            r = compare(prev, cur, roi)
            if r['changed'] or r['lighting']:
                out.append({'from': prev_t, 'to': t, 'lighting': r['lighting'], 'score': r['score'],
                            'boxes': r['boxes']})
        if cur is not None:
            prev, prev_t = cur, t
        t += step
    return out


def bisect(src, start, end, roi, probe=3, gap=2.0, min_span=1.0):
    """在 [start, end] 里找变化时刻。前提：start 与 end 的画面在 roi 内不同。
    每个探测点看 t, t+gap, t+2gap 三帧，多数“和起点不同”才算已变（防路过遮挡）。"""
    ref = src.at(start)
    steps = []

    def changed_at(t):
        votes = 0
        for k in range(probe):
            f = src.at(min(t + k * gap, end))
            if f is not None:
                r = compare(ref, f, roi)
                votes += r['changed'] and not r['lighting']
        return votes * 2 > probe

    if not changed_at(end):
        return {'found': False, 'reason': '区间首尾在该区域没有持续变化（或只是光照变化）', 'steps': 0}
    lo, hi = start, end
    while hi - lo > min_span:
        mid = (lo + hi) / 2
        if changed_at(mid):
            hi = mid
        else:
            lo = mid
        steps.append(round(mid, 1))
    before, after = src.at(lo), src.at(hi)
    r = compare(before, after, roi) if before is not None and after is not None else {}
    return {'found': True, 'before': lo, 'after': hi, 'steps': len(steps), 'boxes': r.get('boxes', [])}


# ---------------- 命令行 ----------------

def _roi(s):
    if not s:
        return None
    v = [float(x) for x in s.split(',')]
    if len(v) != 4 or not all(0 <= x <= 1 for x in v):
        sys.exit('--roi 需要 4 个 0~1 之间的数：x1,y1,x2,y2')
    return v


def _clock(s):
    if not s:
        return None
    h, m, *sec = [int(x) for x in s.split(':')]
    return h * 3600 + m * 60 + (sec[0] if sec else 0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('compare')
    p.add_argument('a'); p.add_argument('b')
    p.add_argument('--roi'); p.add_argument('--out')
    for name in ('scan', 'bisect'):
        p = sub.add_parser(name)
        p.add_argument('--video'); p.add_argument('--images')
        p.add_argument('--start', type=float); p.add_argument('--end', type=float)
        p.add_argument('--roi'); p.add_argument('--clock', help='视频开头对应的墙上时间 HH:MM:SS')
        p.add_argument('--out', help='把变化前后两帧和标框图存到这个目录')
        if name == 'scan':
            p.add_argument('--step', type=float, default=600)
    a = ap.parse_args(argv)

    if a.cmd == 'compare':
        A, B = cv2.imread(a.a), cv2.imread(a.b)
        if A is None or B is None:
            sys.exit('读不到图片')
        r = compare(A, B, _roi(a.roi))
        if a.out and r['boxes']:
            r['diff_image'] = draw(B, r['boxes'], a.out)
        print(json.dumps(r, ensure_ascii=False))
        return

    if not (a.video or a.images):
        sys.exit('需要 --video 或 --images')
    src = Source(a.video, a.images)
    start = src.start() if a.start is None else a.start
    end = src.duration if a.end is None else a.end
    clock = _clock(a.clock)
    roi = _roi(a.roi)
    if a.cmd == 'scan':
        res = scan(src, start, end, a.step, roi)
        for x in res:
            x['from_t'], x['to_t'] = fmt(x['from'], clock), fmt(x['to'], clock)
            if not a.video:
                x['from_file'], x['to_file'] = src.name(x['from']), src.name(x['to'])
        print(json.dumps({'intervals': res, 'samples': int((end - start) / a.step) + 1}, ensure_ascii=False, indent=1))
    else:
        r = bisect(src, start, end, roi)
        if r['found']:
            r['before_t'], r['after_t'] = fmt(r['before'], clock), fmt(r['after'], clock)
            if a.out:
                os.makedirs(a.out, exist_ok=True)
                fb, fa = src.at(r['before']), src.at(r['after'])
                cv2.imwrite(os.path.join(a.out, 'before.jpg'), fb)
                r['images'] = [os.path.join(a.out, 'before.jpg'),
                               draw(fa, r['boxes'], os.path.join(a.out, 'after.jpg'))]
        print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
