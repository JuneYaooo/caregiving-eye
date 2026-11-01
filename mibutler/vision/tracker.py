# -*- coding: utf-8 -*-
"""轻量 IoU 跟踪器: 给检测框稳定 track_id (单相机 3fps 场景够用)
升级路径: ByteTrack (ultralytics), 接口保持 update(dets) -> tracks
"""
import time


class Track:
    __slots__ = ('tid', 'cls', 'bbox', 'score', 'hits', 'misses', 'first_seen',
                 'last_seen', 'centers', 'posture', 'last_posture_ts')

    def __init__(self, tid, det, now):
        self.tid = tid
        self.cls = det['cls']
        self.bbox = det['bbox']
        self.score = det['score']
        self.hits = 1
        self.misses = 0
        self.first_seen = now
        self.last_seen = now
        x1, y1, x2, y2 = det['bbox']
        self.centers = [((x1 + x2) / 2, (y1 + y2) / 2)]
        self.posture = None          # 'stand'/'sit'/'lie' for persons
        self.last_posture_ts = now

    def center(self):
        return self.centers[-1]

    def age(self, now):
        return now - self.first_seen


class SimpleTracker:
    """IoU 贪心匹配 + 最大消失时长; dets: [{'bbox':[x1,y1,x2,y2],'cls':int,'score':float}]"""

    def __init__(self, iou_th=0.3, max_misses=1.5, min_hits=3):
        self.iou_th = iou_th
        self.max_misses = max_misses   # 秒
        self.min_hits = min_hits       # 确认所需命中数
        self.tracks = {}
        self._next_id = 1

    @staticmethod
    def _iou(a, b):
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        ix = max(0, min(ax2, bx2) - max(ax1, bx1))
        iy = max(0, min(ay2, by2) - max(ay1, by1))
        inter = ix * iy
        if inter <= 0:
            return 0.0
        ua = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
        return inter / ua

    def update(self, dets, now=None):
        now = now or time.time()
        # 1. 老轨匹配
        unmatched_dets = list(range(len(dets)))
        for t in list(self.tracks.values()):
            best, best_iou = None, self.iou_th
            for di in unmatched_dets:
                if dets[di]['cls'] != t.cls:
                    continue
                i = self._iou(t.bbox, dets[di]['bbox'])
                if i > best_iou:
                    best, best_iou = di, i
            if best is not None:
                di = best
                unmatched_dets.remove(di)
                t.bbox = dets[di]['bbox']
                t.score = dets[di]['score']
                t.hits += 1
                t.misses = 0
                t.last_seen = now
                x1, y1, x2, y2 = t.bbox
                t.centers.append(((x1 + x2) / 2, (y1 + y2) / 2))
                t.centers = t.centers[-120:]  # 轨迹保留 ~40s @3fps
            else:
                t.misses = now - t.last_seen

        # 1b. 兜底：IoU 没匹配上时按中心距离匹配。跌倒时人框会从“竖长”突变为“横长”，
        #     IoU 很低但中心离得不远；不兜底会被当成新目标，跌倒前的站立历史就丢了。
        for t in list(self.tracks.values()):
            if t.last_seen == now or not unmatched_dets:
                continue
            x1, y1, x2, y2 = t.bbox
            diag = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            cx, cy = t.center()
            best, best_d = None, 0.6 * diag
            for di in unmatched_dets:
                if dets[di]['cls'] != t.cls:
                    continue
                bx1, by1, bx2, by2 = dets[di]['bbox']
                d = (((bx1 + bx2) / 2 - cx) ** 2 + ((by1 + by2) / 2 - cy) ** 2) ** 0.5
                if d < best_d:
                    best, best_d = di, d
            if best is not None:
                unmatched_dets.remove(best)
                t.bbox, t.score = dets[best]['bbox'], dets[best]['score']
                t.hits += 1
                t.misses = 0
                t.last_seen = now
                bx1, by1, bx2, by2 = t.bbox
                t.centers = (t.centers + [((bx1 + bx2) / 2, (by1 + by2) / 2)])[-120:]

        # 2. 新轨
        for di in unmatched_dets:
            t = Track(self._next_id, dets[di], now)
            self._next_id += 1
            self.tracks[t.tid] = t

        # 3. 清理
        for tid in list(self.tracks):
            if self.tracks[tid].misses > self.max_misses:
                del self.tracks[tid]

        return [t for t in self.tracks.values()
                if t.hits >= self.min_hits and t.misses == 0]

    def confirmed(self, t):
        return t.hits >= self.min_hits


def main():
    """自测: 两个框交替移动, 期望 track id 稳定"""
    tr = SimpleTracker(min_hits=2)
    now = 0.0
    dets = [{'bbox': [100, 100, 200, 300], 'cls': 0, 'score': 0.9}]
    for step in range(12):
        box = dets[0]['bbox']
        dets[0]['bbox'] = [box[0] + 5, box[1], box[2] + 5, box[3]]
        ts = tr.update(dets, now=now)
        now += 0.33
        print('step%d ids=%s' % (step, [(t.tid, t.hits) for t in ts]))
    assert len(set(t.tid for t in ts)) == 1, 'track id 应稳定'
    print('tracker OK')


if __name__ == '__main__':
    main()
