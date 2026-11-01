# -*- coding: utf-8 -*-
"""在一段录像上试跌倒模型：按执事运行时的方式（约 5 fps 采样）逐帧打分，打印超过阈值的时刻

用法:
  python scripts/fall_gcn/try_video.py my_clip.mp4
  python scripts/fall_gcn/try_video.py my_clip.mp4 --fps 3 --th 0.6 --crop right

只依赖 onnxruntime，不需要 PyTorch。用家里真实录像检查误报很有用：
坐下、弯腰、躺沙发都不该报；可以自己演示一次缓慢倒地，看能不能报出来。
"""
import argparse, os, sys
import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from mibutler.vision.yolo import YoloOnnx           # noqa: E402
from mibutler.abilities.fall_gcn import FallClassifier  # noqa: E402
from extract import extract                        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('video')
    ap.add_argument('--model', default=os.path.join(ROOT, 'models', 'fall_stgcn.onnx'))
    ap.add_argument('--pose', default=os.path.join(ROOT, 'models', 'yolov8n-pose.onnx'))
    ap.add_argument('--fps', type=float, default=5.0)
    ap.add_argument('--th', type=float, default=0.6)
    ap.add_argument('--crop', choices=['none', 'left', 'right'], default='none')
    args = ap.parse_args()

    clf = FallClassifier(args.model)
    ts, kp, sc = extract(args.video, YoloOnnx(args.pose), args.fps, args.crop)
    peak, hits = 0.0, []
    for t, k, s in zip(ts, kp, sc):
        p = clf.update(0, float(t), k.tolist() if s > 0 else None)
        if p is None:
            continue
        peak = max(peak, p)
        bar = '█' * int(p * 20)
        print('%7.1fs  %.2f %s' % (t, p, bar))
        if p >= args.th:
            hits.append(t)
    print('\n最高概率 %.2f；超过阈值 %.2f 的时刻：%s'
          % (peak, args.th, ', '.join('%.1fs' % t for t in hits) if hits else '无'))


if __name__ == '__main__':
    main()
