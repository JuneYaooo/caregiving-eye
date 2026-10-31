# -*- coding: utf-8 -*-
"""火焰 / 烟雾视觉检测

模型：任意 Ultralytics YOLOv8 火焰烟雾模型导出的 ONNX。推荐
  https://huggingface.co/rabahdev/fire-smoke-yolov8n （D-Fire 数据集，类别 0=smoke 1=fire，AGPL-3.0）
导出：pip install ultralytics && yolo export model=best.pt format=onnx imgsz=640
      然后把 best.onnx 放到 models/fire-smoke-yolov8n.onnx

误报控制：阳光、红色物体、灯光闪烁都可能被误检，所以要求“持续出现”——
  窗口 window_sec 内命中 >= min_hits 次才算疑似；持续 confirm_sec 秒以上才算确认。
"""
import collections, os


class FireSmokeDetector:
    def __init__(self, model_path, class_names=('smoke', 'fire'), score_th=0.45,
                 window_sec=6.0, min_hits=3, confirm_sec=15.0, min_area_frac=0.0005):
        from care_pipeline import YoloOnnx
        self.model = YoloOnnx(model_path)
        self.names = list(class_names)
        self.score_th = score_th
        self.window_sec, self.min_hits = window_sec, min_hits
        self.confirm_sec = confirm_sec
        self.min_area_frac = min_area_frac
        self.hits = collections.deque()   # (ts, label, score, bbox)
        self.first_suspect = None

    @classmethod
    def from_env(cls, cfg):
        path = cfg('FIRE_MODEL', os.path.join(cfg('MODELS_DIR', 'models'), 'fire-smoke-yolov8n.onnx'))
        if not path or not os.path.exists(path):
            return None
        names = [s.strip() for s in cfg('FIRE_CLASSES', 'smoke,fire').split(',') if s.strip()]
        return cls(path, names, score_th=float(cfg('FIRE_SCORE', 0.45)))

    def update(self, frame, now):
        """返回 None / {'level': 'suspect'|'confirmed', 'labels': set, 'bbox': [...], 'score': float}"""
        H, W = frame.shape[:2]
        dets = []
        for d in self.model(frame, self.score_th):
            x1, y1, x2, y2 = d['bbox']
            if (x2 - x1) * (y2 - y1) < self.min_area_frac * W * H:
                continue
            label = self.names[d['cls']] if d['cls'] < len(self.names) else str(d['cls'])
            dets.append((now, label, d['score'], d['bbox']))
        self.hits.extend(dets)
        while self.hits and now - self.hits[0][0] > self.window_sec:
            self.hits.popleft()
        # 同一时刻多个框只算一次命中
        n_frames = len({round(h[0], 2) for h in self.hits})
        if n_frames < self.min_hits:
            if not self.hits:
                self.first_suspect = None
            return None
        if self.first_suspect is None:
            self.first_suspect = now
        best = max(self.hits, key=lambda h: h[2])
        level = 'confirmed' if now - self.first_suspect >= self.confirm_sec else 'suspect'
        return {'level': level, 'labels': {h[1] for h in self.hits},
                'bbox': best[3], 'score': best[2], 'since': self.first_suspect}
