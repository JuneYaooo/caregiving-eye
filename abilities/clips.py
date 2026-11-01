# -*- coding: utf-8 -*-
"""事件视频存证：保存事件发生前后的一小段画面。

环形缓冲持续保留最近 pre_sec 秒的低分辨率画面；trigger() 后继续收集 post_sec 秒，
随后在后台线程写成 mp4。内存占用约为 width×height×3×fps×pre_sec 字节。
"""
import collections, os, threading, time
import cv2


class ClipRecorder:
    def __init__(self, out_dir, pre_sec=10, post_sec=15, fps=5, width=960):
        self.out_dir = out_dir
        self.pre_sec, self.post_sec = pre_sec, post_sec
        self.fps, self.width = fps, width
        self.buf = collections.deque(maxlen=max(1, int(pre_sec * fps)))
        self.active = []          # [{'path','until','frames'}]
        self._last_push = 0
        self._lock = threading.Lock()     # 拉流线程 push，主循环 trigger
        os.makedirs(out_dir, exist_ok=True)

    def _small(self, frame):
        h, w = frame.shape[:2]
        if w <= self.width:
            return frame.copy()
        return cv2.resize(frame, (self.width, int(h * self.width / w)))

    def push(self, frame, now=None):
        """每读一帧调用；内部按 fps 抽帧"""
        now = now or time.time()
        if now - self._last_push < 1.0 / self.fps:
            return
        self._last_push = now
        small = self._small(frame)
        with self._lock:
            self.buf.append(small)
            done = []
            for c in self.active:
                c['frames'].append(small)
                if now >= c['until']:
                    done.append(c)
            for c in done:
                self.active.remove(c)
        for c in done:
            threading.Thread(target=self._write, args=(c,), daemon=True).start()

    def trigger(self, tag, now=None):
        """开始一段存证；同一 tag 正在录制时复用，返回 mp4 路径"""
        now = now or time.time()
        with self._lock:
            for c in self.active:
                if c['tag'] == tag:
                    c['until'] = max(c['until'], now + self.post_sec)
                    return c['path']
            path = os.path.join(self.out_dir, 'clip_%s_%s.mp4' % (tag, time.strftime('%Y%m%d_%H%M%S')))
            self.active.append({'tag': tag, 'path': path, 'until': now + self.post_sec,
                                'frames': list(self.buf)})
        return path

    def _write(self, c):
        frames = c['frames']
        if not frames:
            return
        h, w = frames[0].shape[:2]
        vw = cv2.VideoWriter(c['path'], cv2.VideoWriter_fourcc(*'mp4v'), self.fps, (w, h))
        for f in frames:
            if f.shape[:2] == (h, w):
                vw.write(f)
        vw.release()

    def flush(self):
        with self._lock:
            active, self.active = self.active, []
        for c in active:
            self._write(c)
