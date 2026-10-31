# -*- coding: utf-8 -*-
"""声音事件：摄像头麦克风 → ffmpeg 解成 16 kHz PCM → YAMNet → 事件

模型：YAMNet（Google，AudioSet 521 类，Apache-2.0）的 ONNX 转换版，直接输入波形：
  https://huggingface.co/audiomagic/yamnet-onnx  （yamnet.onnx + yamnet_class_map.csv）
放到 models/yamnet.onnx 与 models/yamnet_class_map.csv。
需要系统已安装 ffmpeg，并且 go2rtc 流带音频轨。

事件分组（按 AudioSet 类名匹配，可在 GROUPS 里调整）：
  smoke_alarm  烟雾 / 火警报警器
  glass        玻璃碎裂、撞击
  distress     尖叫、哭泣、呻吟、呼喊
  baby_cry     婴儿哭
  pet_distress 猫狗嘶叫、哀鸣、长时间吠叫
"""
import collections, csv, os, subprocess, threading, time
import numpy as np

SR = 16000
GROUPS = {
    'smoke_alarm': ['Smoke detector, smoke alarm', 'Fire alarm', 'Alarm', 'Buzzer'],
    'glass': ['Glass', 'Shatter', 'Smash, crash', 'Breaking'],
    'distress': ['Screaming', 'Crying, sobbing', 'Groan', 'Whimper', 'Wail, moan',
                 'Shout', 'Yell'],
    'baby_cry': ['Baby cry, infant cry'],
    'pet_distress': ['Hiss', 'Caterwaul', 'Howl', 'Whimper (dog)', 'Growling'],
}
GROUP_TH = {'smoke_alarm': 0.35, 'glass': 0.3, 'distress': 0.35, 'baby_cry': 0.4,
            'pet_distress': 0.35}


def load_class_map(path):
    names = []
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            names.append(row['display_name'])
    return names


class YamnetOnnx:
    def __init__(self, model_path, class_map_path):
        import onnxruntime as ort
        self.sess = ort.InferenceSession(model_path, providers=['CPUExecutionProvider'])
        self.inp = self.sess.get_inputs()[0].name
        self.names = load_class_map(class_map_path)
        idx = {n: i for i, n in enumerate(self.names)}
        self.groups = {g: [idx[n] for n in ns if n in idx] for g, ns in GROUPS.items()}

    def __call__(self, wav):
        """wav: float32 [-1,1] 单声道 16 kHz；返回 {group: max_score}, top3"""
        scores = self.sess.run(None, {self.inp: wav.astype(np.float32)})[0]
        s = scores.max(axis=0) if scores.ndim == 2 else scores
        out = {g: float(s[ids].max()) if ids else 0.0 for g, ids in self.groups.items()}
        top = [(self.names[i], float(s[i])) for i in np.argsort(-s)[:3]]
        return out, top


class AudioListener(threading.Thread):
    """后台线程：持续拉音频、每 hop 秒分析一次最近 win 秒"""

    def __init__(self, source, model, win=1.0, hop=0.5, on_event=None):
        super().__init__(daemon=True)
        self.source, self.model = source, model
        self.win, self.hop = win, hop
        self.on_event = on_event
        self.recent = collections.deque(maxlen=200)   # (ts, group, score)
        self.alive = True
        self.level_db = -90.0

    @classmethod
    def from_env(cls, cfg, on_event=None):
        md = cfg('MODELS_DIR', 'models')
        mp = cfg('AUDIO_MODEL', os.path.join(md, 'yamnet.onnx'))
        cp = cfg('AUDIO_CLASS_MAP', os.path.join(md, 'yamnet_class_map.csv'))
        src = cfg('AUDIO_SOURCE', cfg('RTSP_URL'))
        if not (src and os.path.exists(mp) and os.path.exists(cp)):
            return None
        return cls(src, YamnetOnnx(mp, cp), on_event=on_event)

    def _open(self):
        cmd = ['ffmpeg', '-loglevel', 'error', '-rtsp_transport', 'tcp', '-i', self.source,
               '-vn', '-ac', '1', '-ar', str(SR), '-f', 's16le', '-']
        if not self.source.startswith('rtsp'):
            cmd = ['ffmpeg', '-loglevel', 'error', '-i', self.source,
                   '-vn', '-ac', '1', '-ar', str(SR), '-f', 's16le', '-']
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def run(self):
        hop_bytes = int(self.hop * SR) * 2
        ring = np.zeros(int(self.win * SR), np.float32)
        while self.alive:
            try:
                proc = self._open()
            except FileNotFoundError:
                print('[audio] 未找到 ffmpeg，声音事件已停用')
                return
            while self.alive:
                raw = proc.stdout.read(hop_bytes)
                if not raw or len(raw) < hop_bytes:
                    break
                chunk = np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0
                ring = np.concatenate([ring[len(chunk):], chunk])
                self.level_db = 20 * np.log10(np.sqrt(np.mean(ring ** 2)) + 1e-9)
                if self.level_db < -60:      # 太安静就不跑模型
                    continue
                groups, top = self.model(ring)
                now = time.time()
                for g, s in groups.items():
                    if s >= GROUP_TH.get(g, 0.4):
                        self.recent.append((now, g, s))
                        if self.on_event:
                            self.on_event(g, s, top)
            proc.kill()
            time.sleep(3)   # 断流重连

    def heard(self, group, within=30.0, min_count=1):
        """最近 within 秒内是否听到某类声音"""
        now = time.time()
        return sum(1 for t, g, _ in self.recent if g == group and now - t <= within) >= min_count

    def stop(self):
        self.alive = False
