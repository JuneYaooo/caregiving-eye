# -*- coding: utf-8 -*-
"""拉流服务：后台线程持续读流，只保留最新一帧

为什么需要它：模型推理比视频帧率慢时，直接在主循环里 cap.read() 会让 OpenCV 缓冲越积越多，
分析的画面会落后现实几十秒。这里用 grab() 跟上实时流，只在需要时 retrieve() 解出图像
（最多 retrieve_fps 帧/秒），省掉大部分颜色转换和内存拷贝。

断流时按 1s、2s、4s…最多 30s 退避重连。
来源是本地录像文件时按原始帧率播放（用于拿录像校准规则），播完会从头再放。
"""
import threading, time
import cv2


class FrameSource(threading.Thread):
    def __init__(self, source, retrieve_fps=5.0, on_frame=None):
        super().__init__(daemon=True, name='frames')
        self.source = source
        self.retrieve_fps = retrieve_fps
        self.on_frame = on_frame          # 每解出一帧回调 (frame, ts)，用于视频存证缓冲
        self._lock = threading.Lock()
        self._frame, self._ts, self.seq = None, 0.0, 0
        self.alive = True
        self.connected = False
        self.reconnects = 0
        self.fps = 0.0
        self.is_file = not str(source).lower().startswith(('rtsp', 'http', 'rtmp', 'udp', 'tcp'))

    def _open(self):
        cap = cv2.VideoCapture(self.source)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def run(self):
        backoff = 1.0
        while self.alive:
            cap = self._open()
            if not cap.isOpened():
                self.connected = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                self.reconnects += 1
                continue
            last_ret, n, t_fps = 0.0, 0, time.time()
            fails = 0
            frame_dt = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 15) if self.is_file else 0
            next_t = time.time()
            while self.alive:
                if frame_dt:
                    next_t += frame_dt
                    time.sleep(max(0.0, next_t - time.time()))
                if not cap.grab():
                    if self.is_file:
                        break
                    fails += 1
                    if fails > 25:
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                n += 1
                now = time.time()
                if now - t_fps >= 5:
                    self.fps, n, t_fps = n / (now - t_fps), 0, now
                if now - last_ret < 1.0 / self.retrieve_fps:
                    continue
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue
                last_ret = now
                self.connected, backoff = True, 1.0
                with self._lock:
                    self._frame, self._ts = frame, now
                    self.seq += 1
                if self.on_frame:
                    try:
                        self.on_frame(frame, now)
                    except Exception as e:
                        print('[frames] on_frame 出错：', e)
            cap.release()
            self.connected = False
            self.reconnects += 1
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def latest(self):
        with self._lock:
            return self._frame, self._ts, self.seq

    def stale_for(self, now=None):
        return (now or time.time()) - self._ts if self._ts else float('inf')

    def stop(self):
        self.alive = False
