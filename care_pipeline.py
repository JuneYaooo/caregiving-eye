# -*- coding: utf-8 -*-
"""家庭照护视觉管线 v0：RTSP/文件流 -> 人/猫检测 -> 跌倒判定 -> 猫进禁区报警
依赖: opencv-python-headless, onnxruntime, numpy (py3.9)
用法:
  python care_pipeline.py --test-image models/bus.jpg          # 冒烟测试
  python care_pipeline.py --source rtsp://... --no-display      # 生产模式
  python care_pipeline.py --source test.mp4 --zones zones.json --webhook http://...
"""
import argparse, json, os, time
import numpy as np
import cv2
import onnxruntime as ort

PERSON, CAT, DOG = 0, 15, 16
CLASS_NAMES = {PERSON: 'person', CAT: 'cat', DOG: 'dog'}

# ---- 跌倒判定参数（v0 启发式，需真实画面校准） ----
FALL_CFG = {
    'stand_aspect_max': 0.9,     # 站立态 bbox 宽高比上限
    'lying_aspect_min': 1.1,     # 倒地段 bbox 宽高比下限
    'fall_aspect_min': 1.15,     # 触发跌倒事件的最小宽高比
    'drop_frac': 0.18,           # 中心垂直骤降占帧高比例
    'transition_secs': 2.0,      # 站->倒转换时间窗
    'min_score': 0.4,            # 人形检测分阈值
}
CAT_CFG = {'min_score': 0.45, 'cooldown_secs': 90}


def letterbox(img, new=640):
    h, w = img.shape[:2]
    r = min(new / h, new / w)
    nh, nw = int(h * r), int(w * r)
    top, left = (new - nh) // 2, (new - nw) // 2
    canvas = np.full((new, new, 3), 114, np.uint8)
    canvas[top:top + nh, left:left + nw] = cv2.resize(img, (nw, nh))
    return canvas, r, left, top


class YoloOnnx:
    def __init__(self, path, providers=None):
        so = ort.SessionOptions()
        self.sess = ort.InferenceSession(path, so, providers or ['CPUExecutionProvider'])
        self.input = self.sess.get_inputs()[0].name
        out = self.sess.get_outputs()[0].shape
        self.is_pose = len(out) >= 2 and out[1] == 56

    def __call__(self, img, score_th=0.35):
        im, r, dx, dy = letterbox(img)
        x = im[:, :, ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        pred = self.sess.run(None, {self.input: x})[0][0]  # (84|56, 8400)
        pred = pred.T
        boxes_raw = pred[:, :4]
        cls_ids = None
        kpts = None
        if self.is_pose:
            scores = pred[:, 4]
            kpts = pred[:, 5:].reshape(-1, 17, 3)
        else:
            scores = pred[:, 4:].max(axis=1)
            cls_ids = pred[:, 4:].argmax(axis=1)
            kpts = None
        keep = scores > score_th
        boxes_raw, scores = boxes_raw[keep], scores[keep]
        cls_ids = cls_ids[keep] if cls_ids is not None else None
        kpts = kpts[keep] if kpts is not None else None
        if not len(scores):
            return []
        xy = boxes_raw[:, :2] - boxes_raw[:, 2:] / 2
        wh = boxes_raw[:, 2:]
        boxes_px = np.concatenate([xy, wh], axis=1)
        idx = cv2.dnn.NMSBoxes(boxes_px.tolist(), scores.tolist(), score_th, 0.45)
        out = []
        for i in np.array(idx).flatten():
            cx, cy, w, h = boxes_px[i]
            x1 = (cx - w / 2 - dx) / r; y1 = (cy - h / 2 - dy) / r
            x2 = (cx + w / 2 - dx) / r; y2 = (cy + h / 2 - dy) / r
            det = {'bbox': [float(x1), float(y1), float(x2), float(y2)],
                   'score': float(scores[i]), 'cls': int(cls_ids[i]) if not self.is_pose else PERSON}
            if kpts is not None:
                k = kpts[i]
                det['kpts'] = [[float((p[0] - dx) / r), float((p[1] - dy) / r), float(p[2])] for p in k]
            out.append(det)
        return out


def in_poly(pt, poly):
    return cv2.pointPolygonTest(np.array(poly, np.float32), pt, False) >= 0


class FallDetector:
    """跟踪单个人：站立->倒地的速度+姿态启发式"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = {}  # track简易版：只跟踪"最大的人"

    def update(self, persons, frame_h, ts):
        events = []
        if not persons:
            self.state = {}
            return events
        p = max(persons, key=lambda d: (d['bbox'][2] - d['bbox'][0]) * (d['bbox'][3] - d['bbox'][1]))
        x1, y1, x2, y2 = p['bbox']
        w, h = x2 - x1, y2 - y1
        aspect = w / max(h, 1e-6)
        cy = (y1 + y2) / 2
        s = self.state
        lying = aspect > self.cfg['lying_aspect_min']
        if s and s.get('standing') and lying and s.get('last_stand_ts') \
                and ts - s['last_stand_ts'] < self.cfg['transition_secs'] \
                and cy - s.get('last_cy', cy) > self.cfg['drop_frac'] * frame_h:
            events.append({'type': 'fall', 'score': float(p['score']), 'bbox': p['bbox'],
                           'note': 'aspect=%.2f drop=%.2f' % (aspect, cy - s.get('last_cy', cy))})
        s.update({'standing': aspect < self.cfg['stand_aspect_max'],
                  'last_stand_ts': ts if aspect < self.cfg['stand_aspect_max'] else s.get('last_stand_ts'),
                  'last_cy': cy, 'last_ts': ts, 'lying_since': ts if lying else None})
        if lying and s.get('lying_since') and ts - s['lying_since'] > 30 \
                and ts - s.get('last_lie_alert', 0) > 60:
            s['last_lie_alert'] = ts
            events.append({'type': 'lying_long', 'score': float(p['score']), 'bbox': p['bbox'],
                           'note': '倒地静止超过30s'})
        return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default='')
    ap.add_argument('--det', default='models/yolov8n.onnx')
    ap.add_argument('--pose', default='models/yolov8n-pose.onnx')
    ap.add_argument('--zones', default='zones.json')
    ap.add_argument('--webhook', default='')
    ap.add_argument('--no-display', action='store_true')
    ap.add_argument('--test-image', default='')
    ap.add_argument('--fps', type=float, default=3.0, help='分析帧率')
    args = ap.parse_args()

    det_model = YoloOnnx(args.det)
    pose_model = YoloOnnx(args.pose) if os.path.exists(args.pose) else None
    print('[init] det=%s pose=%s' % (args.det, 'yes' if pose_model else 'no'))

    if args.test_image:
        img = cv2.imread(args.test_image)
        dets = det_model(img)
        for d in dets:
            print('det cls=%s score=%.2f bbox=%s' % (CLASS_NAMES.get(d['cls'], d['cls']), d['score'],
                                                     [round(v) for v in d['bbox']]))
        if pose_model:
            pd = pose_model(img, 0.3)
            print('pose persons:', len(pd))
        return

    zones = {}
    if os.path.exists(args.zones):
        zones = json.load(open(args.zones, encoding='utf-8')).get('cat_zones', [])
        print('[init] cat zones:', [z['name'] for z in zones])

    os.makedirs('events', exist_ok=True)
    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print('ERROR: cannot open source', args.source)
        return
    fall = FallDetector(FALL_CFG)
    last_ana, zone_last = 0.0, {}
    frame_id = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.5)
            cap.release()
            cap = cv2.VideoCapture(args.source)
            continue
        frame_id += 1
        now = time.time()
        if now - last_ana < 1.0 / args.fps:
            continue
        last_ana = now
        dets = det_model(frame)
        persons = [d for d in dets if d['cls'] == PERSON and d['score'] > FALL_CFG['min_score']]
        cats = [d for d in dets if d['cls'] in (CAT, DOG) and d['score'] > CAT_CFG['min_score']]

        alerts = fall.update(persons, frame.shape[0], now)

        for d in cats:
            cx, cy = (d['bbox'][0] + d['bbox'][2]) / 2, (d['bbox'][1] + d['bbox'][3]) / 2
            for z in zones:
                if in_poly((cx, cy), z['poly']) and now - zone_last.get(z['name'], 0) > CAT_CFG['cooldown_secs']:
                    zone_last[z['name']] = now
                    alerts.append({'type': 'cat_zone', 'zone': z['name'], 'score': float(d['score']),
                                   'bbox': d['bbox'], 'note': 'pet=%s' % CLASS_NAMES[d['cls']]})

        for a in alerts:
            a['ts'] = time.strftime('%Y-%m-%d %H:%M:%S')
            snap = 'events/%s_%d.jpg' % (a['type'], frame_id)
            cv2.imwrite(snap, frame)
            a['snapshot'] = snap
            line = json.dumps(a, ensure_ascii=False)
            print('[ALERT]', line)
            with open('events/alerts.jsonl', 'a', encoding='utf-8') as f:
                f.write(line + '\n')
            if args.webhook:
                try:
                    import urllib.request
                    req = urllib.request.Request(args.webhook, data=json.dumps(
                        {'msgtype': 'text', 'text': {'content': str(a)}}).encode(),
                        headers={'Content-Type': 'application/json'})
                    urllib.request.urlopen(req, timeout=5)
                except Exception as e:
                    print('webhook fail:', e)

        if not args.no_display:
            for d in persons + cats:
                x1, y1, x2, y2 = map(int, d['bbox'])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.imshow('care', frame)
            if cv2.waitKey(1) == 27:
                break
    cap.release()


if __name__ == '__main__':
    main()
