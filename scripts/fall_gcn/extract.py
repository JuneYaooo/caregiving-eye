# -*- coding: utf-8 -*-
"""第 1 步：把视频变成关键点序列（用的就是执事运行时的 YOLOv8n-pose，训练和实际使用看到的骨架一致）

用法:
  python scripts/fall_gcn/extract.py --videos "data/urfall/*.mp4" --crop right --out data/kpts
  python scripts/fall_gcn/extract.py --videos "my_clips/*.mp4" --out data/kpts

每个视频输出一个 <名字>.npz：ts (F,) 秒、kpts (F,17,3)、score (F,)，没检出人的帧 kpts 全 0。
画面里有多个人时取置信度最高的那个（公开数据集都是单人）。只保存坐标，不保存画面。
--crop right：UR Fall 的 mp4 左半是深度图、右半是彩色画面，只取右半。
"""
import argparse, glob, os, sys
import numpy as np
import cv2

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
from care_pipeline import YoloOnnx   # noqa: E402


def extract(path, model, fps=10.0, crop='none'):
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(src_fps / fps)))
    ts, kp, sc = [], [], []
    i = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            w = frame.shape[1]
            if crop == 'right':
                frame = frame[:, w // 2:]
            elif crop == 'left':
                frame = frame[:, :w // 2]
            dets = model(frame, 0.3)
            if dets:
                d = max(dets, key=lambda d: d['score'])
                kp.append(np.array(d['kpts'], np.float32))
                sc.append(d['score'])
            else:
                kp.append(np.zeros((17, 3), np.float32))
                sc.append(0.0)
            ts.append(i / src_fps)
        i += 1
    cap.release()
    return np.array(ts, np.float64), np.array(kp, np.float32).reshape(-1, 17, 3), np.array(sc, np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--videos', required=True, help='视频通配符，如 "data/urfall/*.mp4"')
    ap.add_argument('--out', default='data/kpts')
    ap.add_argument('--pose', default=os.path.join(ROOT, 'models', 'yolov8n-pose.onnx'))
    ap.add_argument('--fps', type=float, default=10.0, help='抽帧频率，默认 10')
    ap.add_argument('--crop', choices=['none', 'left', 'right'], default='none')
    args = ap.parse_args()

    model = YoloOnnx(args.pose)
    os.makedirs(args.out, exist_ok=True)
    files = sorted(glob.glob(args.videos))
    if not files:
        sys.exit('没找到视频：%s' % args.videos)
    for n, f in enumerate(files, 1):
        out = os.path.join(args.out, os.path.splitext(os.path.basename(f))[0] + '.npz')
        if os.path.exists(out):
            continue
        ts, kp, sc = extract(f, model, args.fps, args.crop)
        np.savez_compressed(out, ts=ts, kpts=kp, score=sc)
        print('[%d/%d] %s：%d 帧，检出人 %d 帧' % (n, len(files), os.path.basename(f), len(ts), int((sc > 0).sum())))


if __name__ == '__main__':
    main()
