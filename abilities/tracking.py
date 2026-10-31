# -*- coding: utf-8 -*-
"""跟踪器工厂

TRACKER=bytetrack 时使用 roboflow/supervision 的 ByteTrack（MIT，纯 Python，无需 PyTorch）：
  pip install "supervision>=0.20,<0.30"
遮挡、交叉走动时 ID 更稳。未安装或 TRACKER=iou 时使用内置 SimpleTracker。
两者对外接口一致：update(dets, now) -> [Track]，Track 有 tid / cls / bbox / hits / center()。
"""
import numpy as np
from simple_tracker import SimpleTracker, Track


class ByteTrackAdapter:
    def __init__(self, fps=3, min_hits=3):
        import supervision as sv
        self.sv = sv
        self.bt = sv.ByteTrack(frame_rate=int(max(fps, 1)))
        self.min_hits = min_hits
        self.tracks = {}

    def update(self, dets, now):
        sv = self.sv
        if dets:
            d = sv.Detections(xyxy=np.array([x['bbox'] for x in dets], np.float32),
                              confidence=np.array([x['score'] for x in dets], np.float32),
                              class_id=np.array([x['cls'] for x in dets], int))
        else:
            d = sv.Detections.empty()
        out = self.bt.update_with_detections(d)
        seen = set()
        for xyxy, conf, cls, tid in zip(out.xyxy, out.confidence, out.class_id, out.tracker_id):
            tid = int(tid)
            seen.add(tid)
            det = {'bbox': [float(v) for v in xyxy], 'score': float(conf), 'cls': int(cls)}
            t = self.tracks.get(tid)
            if t is None:
                t = self.tracks[tid] = Track(tid, det, now)
            else:
                t.bbox, t.score = det['bbox'], det['score']
                t.hits += 1
                t.last_seen, t.misses = now, 0
                x1, y1, x2, y2 = t.bbox
                t.centers = (t.centers + [((x1 + x2) / 2, (y1 + y2) / 2)])[-120:]
        for tid in list(self.tracks):
            if tid not in seen:
                self.tracks[tid].misses = now - self.tracks[tid].last_seen
                if self.tracks[tid].misses > 5:
                    self.tracks.pop(tid)
        return [t for tid, t in self.tracks.items() if tid in seen and t.hits >= self.min_hits]


def make_tracker(kind='iou', fps=3):
    if kind == 'bytetrack':
        try:
            return ByteTrackAdapter(fps=fps)
        except Exception as e:
            print('[tracker] ByteTrack 不可用（%s），回退到 IoU 跟踪' % e)
    return SimpleTracker()
