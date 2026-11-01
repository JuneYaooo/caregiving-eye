# -*- coding: utf-8 -*-
"""第 2 步：训练 ST-GCN 跌倒分类器并导出 ONNX

用法:
  pip install -r scripts/fall_gcn/requirements.txt
  python scripts/fall_gcn/train.py --kpts data/kpts --labels data/urfall/labels.csv \
         --out models/fall_stgcn.onnx

可以给多个 --labels（例如 UR Fall + 自己标的家里录像），name 对应 --kpts 下的 <name>.npz。
标签表 labels.csv：name,fall_start,fall_end（秒）。fall_start 是开始失去平衡，
fall_end 是身体着地；一段视频有多次跌倒就写多行；日常活动视频两列留空。

窗口怎么算正负样本（窗口长 WINDOW_SEC=4 秒，以窗口结束时刻 t 为准）：
  正样本  fall_end - 0.2 ≤ t ≤ fall_end + 2.0     窗口里包含了“失衡 → 着地”的过程
  忽略    fall_start < t < fall_end - 0.2           跌到一半，标哪类都不合适
          fall_end + 2.0 < t < fall_end + 4.0       跌倒过程正在移出窗口
  负样本  其余时刻，以及所有日常活动视频
训练时随机丢帧（模拟 2.5–10 fps 的检测频率）、丢关键点、左右镜像、缩放、小角度旋转。

家里的样本：执事每次报跌倒 / 疑似跌倒都会把前后约 12 秒的关键点存到 events/samples，
主人回复“没事”标为误报（label 0）、回复“属实”标为真跌倒（label 1），也可以直接改 .json 里的 label。
加 --samples events/samples 一起训练；未标注的样本跳过。误报样本是最有价值的负样本。

先按视频划出 20% 做验证并打印指标，再用全部数据按同样轮数重训一遍导出（--no-final 跳过）。
"""
import argparse, csv, os, random, sys
import numpy as np
import torch
import torch.nn as nn

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..')))
from stgcn import STGCN                                                     # noqa: E402
from mibutler.abilities.fall_gcn import resample, normalize, WINDOW_SEC, T, V, FLIP  # noqa: E402

POS_BEFORE, POS_AFTER = 0.2, 2.0
STEP = 0.1          # 窗口滑动步长（秒）
NOISE = np.random.RandomState(0)


def load_samples(sample_dir):
    """读取执事保存的家里跌倒样本（只用已标注的）"""
    import glob, json
    clips = []
    for meta_path in sorted(glob.glob(os.path.join(sample_dir, 'fall_*.json'))):
        meta = json.load(open(meta_path, encoding='utf-8'))
        if meta.get('label') not in (0, 1):
            continue
        npz = meta_path[:-5] + '.npz'
        if not os.path.exists(npz):
            continue
        z = np.load(npz)
        t = float(meta['t_event'])
        falls = [(t - 1.0, t)] if meta['label'] == 1 else []
        ts, kp = pad(z['ts'], z['kpts'])
        clips.append({'name': os.path.basename(meta_path)[:-5], 'ts': ts, 'kpts': kp, 'falls': falls})
    print('家里样本：%d 段（属实 %d / 误报 %d）' % (
        len(clips), sum(1 for c in clips if c['falls']), sum(1 for c in clips if not c['falls'])))
    return clips


def load(kpts_dir, label_files):
    falls = {}
    for lf in label_files:
        for r in csv.DictReader(open(lf, encoding='utf-8')):
            falls.setdefault(r['name'], [])
            if r.get('fall_start') and r.get('fall_end'):
                falls[r['name']].append((float(r['fall_start']), float(r['fall_end'])))
    clips = []
    for name, fl in sorted(falls.items()):
        p = os.path.join(kpts_dir, name + '.npz')
        if not os.path.exists(p):
            print('缺少关键点文件，跳过：', p)
            continue
        z = np.load(p)
        ts, kp = pad(z['ts'], z['kpts'])
        clips.append({'name': name, 'ts': ts, 'kpts': kp, 'falls': fl})
    return clips


def pad(ts, kp, before=3.0, after=2.0, fps=10.0):
    """公开数据集的片段很短，跌倒前常常不到 1 秒。在开头补一段“保持第一帧姿势不动”、
    结尾补一段“保持最后一帧姿势不动”（加一点抖动），让 4 秒窗口凑得满"""
    if not len(ts):
        return ts, kp
    rs = np.random.RandomState(len(ts))
    valid = np.where(kp[:, :, 2].max(1) > 0)[0]
    if not len(valid):
        return ts, kp
    first, last = kp[valid[0]], kp[valid[-1]]
    nb, na = int(before * fps), int(after * fps)
    def jitter(k, n):
        out = np.repeat(k[None], n, 0).copy()
        out[:, :, :2] += rs.normal(0, 1.0, out[:, :, :2].shape)
        return out
    t_b = ts[0] - np.arange(nb, 0, -1) / fps
    t_a = ts[-1] + np.arange(1, na + 1) / fps
    return (np.concatenate([t_b, ts, t_a]),
            np.concatenate([jitter(first, nb), kp, jitter(last, na)]).astype(np.float32))


def label_at(t, falls):
    """1 正 / 0 负 / None 忽略"""
    for s, e in falls:
        if e - POS_BEFORE <= t <= e + POS_AFTER:
            return 1
        if s < t < e - POS_BEFORE or e + POS_AFTER < t < e + WINDOW_SEC:
            return None
    return 0


def windows(clip):
    """枚举 (t_end, label)；窗口可以伸出视频开头，只要有效帧够多"""
    ts = clip['ts']
    if not len(ts):
        return []
    out = []
    t = ts[0] + WINDOW_SEC
    while t <= ts[-1] + 1e-6:
        y = label_at(t, clip['falls'])
        if y is not None:
            out.append((t, y))
        t += STEP
    return out


def samples_of(clip, t_end, keep=1.0, kp_drop=0.0, rng=None):
    ts, kp = clip['ts'], clip['kpts']
    lo = np.searchsorted(ts, t_end - WINDOW_SEC - 1.0)
    hi = np.searchsorted(ts, t_end, side='right')
    s = []
    for i in range(lo, hi):
        if rng is not None and rng.random() > keep:
            continue
        k = kp[i]
        if k[:, 2].max() <= 0:
            s.append((ts[i], None))
            continue
        if kp_drop and rng is not None:
            k = k.copy()
            k[np.array([rng.random() for _ in range(V)]) < kp_drop, 2] = 0
        s.append((ts[i], k))
    return s


def augment(x, rng):
    """x: (3,T,V) 归一化坐标"""
    x = x.copy()
    if rng.random() < 0.5:
        x = x[:, :, FLIP]
        x[0] = -x[0]
    a = np.radians(rng.uniform(-10, 10))
    c, s = np.cos(a), np.sin(a)
    px, py = x[0].copy(), x[1].copy()
    x[0], x[1] = c * px - s * py, s * px + c * py
    x[:2] *= rng.uniform(0.85, 1.15)
    x[:2] += NOISE.normal(0, 0.01, x[:2].shape)
    x[:2] *= (x[2:3] > 0)
    return x.astype(np.float32)


def make_batch(items, rng, train):
    X, Y, kept = [], [], []
    for clip, t_end, y in items:
        if train:
            s = samples_of(clip, t_end, keep=rng.uniform(0.25, 1.0), kp_drop=0.05, rng=rng)
        else:
            s = samples_of(clip, t_end, keep=0.5, rng=random.Random(int(t_end * 100)))   # 验证固定为约 5 fps
        win, cover = resample(s, t_end)
        if cover < 0.5:
            continue
        x = normalize(win)
        X.append(augment(x, rng) if train else x)
        Y.append(y)
        kept.append(t_end)
    if not X:
        return None, None, []
    return torch.tensor(np.stack(X)), torch.tensor(Y), kept


def fit(train_clips, epochs, seed=0, log=True):
    rng = random.Random(seed)
    NOISE.seed(seed)
    torch.manual_seed(seed)
    items = [(c, t, y) for c in train_clips for t, y in windows(c)]
    pos = [i for i in items if i[2] == 1]
    neg = [i for i in items if i[2] == 0]
    if log:
        print('训练窗口：正 %d / 负 %d（来自 %d 段视频）' % (len(pos), len(neg), len(train_clips)))
    model = STGCN()
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-3)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, epochs)
    w = torch.tensor([1.0, 2.0])     # 漏报比误报代价大
    lossf = nn.CrossEntropyLoss(weight=w, label_smoothing=0.05)
    for ep in range(epochs):
        model.train()
        # 每轮负样本下采样到正样本的 3 倍，保证每轮都见到各类日常动作
        batch_items = pos + rng.sample(neg, min(len(neg), 3 * len(pos)))
        rng.shuffle(batch_items)
        tot, n = 0.0, 0
        for i in range(0, len(batch_items), 64):
            X, Y, _ = make_batch(batch_items[i:i + 64], rng, train=True)
            if X is None:
                continue
            loss = lossf(model(X), Y)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += float(loss) * len(Y)
            n += len(Y)
        sched.step()
        if log and (ep + 1) % 10 == 0:
            print('  epoch %d  loss %.4f' % (ep + 1, tot / max(n, 1)))
    return model


@torch.no_grad()
def evaluate(model, clips, th=0.6):
    """视频级指标：跌倒视频在 [fall_start, fall_end+2] 内任一窗口超过阈值算检出；
    日常视频（及跌倒视频跌倒前的部分）任一窗口超过阈值算误报"""
    model.eval()
    rng = random.Random(0)
    hit = miss = fp = tn = 0
    win_stats = []
    for c in clips:
        items = [(c, t, y) for t, y in windows(c)]
        X, Y, tt = make_batch(items, rng, train=False)   # tt：保留下来的窗口结束时刻
        if X is None:
            continue
        p = torch.softmax(model(X), 1)[:, 1].numpy()
        win_stats += list(zip(p, Y.numpy()))
        if c['falls']:
            s, e = c['falls'][0]
            inside = [pi for pi, t in zip(p, tt) if s <= t <= e + POS_AFTER]
            before = [pi for pi, t in zip(p, tt) if t <= s]
            if inside and max(inside) >= th:
                hit += 1
            else:
                miss += 1
            if before and max(before) >= th:
                fp += 1
        else:
            if max(p) >= th:
                fp += 1
            else:
                tn += 1
    ps = np.array(win_stats)
    acc = float(((ps[:, 0] >= th) == ps[:, 1]).mean()) if len(ps) else 0
    n_fall = hit + miss
    n_adl = sum(1 for c in clips if not c['falls'])
    print('  验证：跌倒视频检出 %d/%d；日常视频误报 %d/%d（含跌倒视频跌倒前的片段误报）；窗口准确率 %.3f'
          % (hit, n_fall, fp, n_adl + n_fall, acc))
    return hit, n_fall, fp


def export(model, path):
    model.eval()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    dummy = torch.zeros(1, 3, T, V)
    torch.onnx.export(model, dummy, path, input_names=['skeleton'], output_names=['logits'],
                      dynamic_axes={'skeleton': {0: 'n'}, 'logits': {0: 'n'}}, opset_version=17,
                      dynamo=False)
    print('已导出：%s' % path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kpts', default='data/kpts')
    ap.add_argument('--labels', action='append', default=[])
    ap.add_argument('--samples', action='append', default=[], help='执事保存的样本目录，如 events/samples')
    ap.add_argument('--out', default='models/fall_stgcn.onnx')
    ap.add_argument('--epochs', type=int, default=60)
    ap.add_argument('--val-frac', type=float, default=0.2)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--no-final', action='store_true', help='不用全部数据重训，直接导出验证用的模型')
    args = ap.parse_args()

    torch.set_num_threads(max(1, os.cpu_count() // 2))
    clips = load(args.kpts, args.labels)
    for d in args.samples:
        clips += load_samples(d)
    if not clips:
        sys.exit('没有可用数据')
    rng = random.Random(args.seed)
    fall_c = [c for c in clips if c['falls']]
    adl_c = [c for c in clips if not c['falls']]
    rng.shuffle(fall_c)
    rng.shuffle(adl_c)
    nf, na = int(len(fall_c) * args.val_frac), int(len(adl_c) * args.val_frac)
    val = fall_c[:nf] + adl_c[:na]
    train = fall_c[nf:] + adl_c[na:]
    model = fit(train, args.epochs, args.seed)
    if val:
        print('验证集：%s' % ' '.join(c['name'] for c in val))
        for th in (0.5, 0.6, 0.7, 0.8, 0.9):
            print(' 阈值 %.1f' % th)
            evaluate(model, val, th)
    if not args.no_final and val:
        print('用全部 %d 段视频重训…' % len(clips))
        model = fit(clips, args.epochs, args.seed, log=False)
    export(model, args.out)


if __name__ == '__main__':
    main()
