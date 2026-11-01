# -*- coding: utf-8 -*-
"""组装：把模型、服务、能力、研判层装在一起，按值班表运行（python main.py run）

  App.__init__   加载模型、区域、各能力模块；可选能力缺模型或依赖时自动跳过
  服务           拉流线程（只留最新帧）、声音线程（安静时不跑模型），由看门狗任务拉起
  signal()       各任务发现情况都调它：附上画面格子、交给 IncidentManager 合并 / 升级 / 存证 / 通报
  run()          排班（duties.roster）→ 主循环 → 退出时保存

每项任务具体做什么，看 mibutler/duties/；调度、研判细节看 docs/brain.md。
"""
import os, time
from functools import partial
import cv2
from mibutler.config import cfg
from mibutler.zones import load_zones, in_poly, cell_of
from mibutler.butler import Butler, HOME, MODE_CN
from mibutler.brain.runtime import Scheduler
from mibutler.brain.frames import FrameSource
from mibutler.brain.gate import MotionGate, AlertLevel, IDLE
from mibutler.brain.incidents import IncidentManager, RANK
from mibutler.brain.ptz import PtzController, ViewCheck
from mibutler.vision.yolo import YoloOnnx
from mibutler.vision.tracking import make_tracker
from mibutler.abilities.clips import ClipRecorder
from mibutler.abilities.fire import FireSmokeDetector
from mibutler.abilities.audio import AudioListener
from mibutler.abilities.child import ChildSafety
from mibutler.abilities.illness import IllnessMonitor
from mibutler.abilities.activity import ActivityLog
from mibutler.abilities.fall import PostureSM, MobilityFilter, FallSamples
from mibutler.abilities.fall_gcn import FallClassifier, FallFusion
from mibutler.abilities.routine import RoutineMonitor
from mibutler import duties


class Ctx:
    """一次运行里各任务共享的状态（只在主循环线程里修改）"""

    def __init__(self, zones):
        self.zones = zones
        self.mode, self.level = HOME, IDLE
        self.frame, self.frame_ts, self.frame_seq, self.motion_seq = None, 0.0, -1, -1
        self.W, self.H = 1, 1
        self.det_seq = 0
        self.tracks, self.persons, self.pets = [], [], []
        self.kpts, self.posture = {}, {}
        self.fall_ts = {}
        self.zone_enter = {}
        self.last_person_ts = time.time()
        self.person_seen_ts = None        # 本次运行里真正看到人的最近时间（作息判断用）
        self.centers = {}                 # tid -> 最近位置，跌倒待定时目标可能已经丢失
        self.zones_valid = True
        self.started = time.time()



def furniture_acc(app, pt, W, H, ts):
    """位置级家具记忆: 格子被占据超过运行时长80%(运行>=30min) → 家具格"""
    from mibutler.zones import cell_of
    dt = 0.0
    if app._last_tick_ts:
        dt = min(10.0, max(0.0, ts - app._last_tick_ts))
    app._last_tick_ts = ts
    app.runtime_sec += dt
    # 12x8 细网格 (比误报学习的 6x4 更细, 避免家具格误伤真人活动区)
    col = min(int(pt[0] / max(W, 1) * 12), 11)
    row = min(int(pt[1] / max(H, 1) * 8), 7)
    cell = 'f%d_%d' % (col, row)
    app.furn_cells[cell] = app.furn_cells.get(cell, 0) + dt
    if ts - app._last_furn_save > 60:
        json.dump({'runtime_sec': app.runtime_sec, 'cells': app.furn_cells},
                  open(app.furn_path, 'w', encoding='utf-8'))
        app._last_furn_save = ts
    occupied = app.furn_cells.get(cell, 0)
    return app.runtime_sec >= 1800 and occupied > 0.8 * min(app.runtime_sec, 6 * 3600)


class App:
    def __init__(self, args):
        self.args = args
        self.ev_dir = args.events_dir
        os.makedirs(self.ev_dir, exist_ok=True)

        # ---------- 模型 ----------
        self.det = YoloOnnx(args.det)
        self.pose = YoloOnnx(args.pose) if os.path.exists(args.pose) else None

        # ---------- 区域与共享状态 ----------
        self.zones = load_zones(args.zones)
        self.ctx = Ctx(self.zones)

        # ---------- 执事与大脑 ----------
        self.butler = Butler(events_path=os.path.join(self.ev_dir, 'alerts.jsonl'))
        self.incidents = IncidentManager(self.butler, self.ev_dir,
                                         remind_sec=float(cfg('REMIND_SEC', 180)),
                                         max_reminds=int(cfg('MAX_REMINDS', 5)))
        self.butler.incidents = self.incidents
        self.butler.on_command = self.on_command
        self.incidents.on_ack.append(self.on_ack)
        self.sched = Scheduler(io_workers=2, budget=float(cfg('TICK_BUDGET', 0.6)))
        self.gate = MotionGate()
        self.level = AlertLevel(hold=float(cfg('CALM_DOWN_SEC', 30)))
        self.ptz = PtzController(enabled=args.follow)
        self.view = ViewCheck(os.path.splitext(args.zones)[0] + '_ref.jpg', zones_path=args.zones)

        # ---------- 能力 ----------
        self.tracker = make_tracker(cfg('TRACKER', 'iou'), fps=3,
                                    max_misses=duties.DETECT_EVERY[IDLE] * 3)
        self.posture = PostureSM()
        self.mobility = MobilityFilter.from_env(cfg)
        self.qualified = set()   # 获得人形资格的 track id (动过 + 关键点可信)
        # 家具格子记忆: 某格被"人形"占据超过运行时长80%(且运行>=30min) → 该格是雕像/挂衣等静物
        self.furn_path = os.path.join(events_dir, 'furniture_cells.json') if             (events_dir := getattr(args, 'events_dir', cfg('EVENTS_DIR', 'events'))) else 'events/furniture_cells.json'
        try:
            _f = json.load(open(self.furn_path, encoding='utf-8'))
            self.furn_cells = _f.get('cells', {})
            self.runtime_sec = float(_f.get('runtime_sec', 0))
        except Exception:
            self.furn_cells, self.runtime_sec = {}, 0.0
        self._last_tick_ts = None
        self.t_furniture = {}   # tid -> 是否家具格 (每轮重算)
        self._last_furn_save = 0.0
        self.samples = FallSamples(os.path.join(self.ev_dir, 'samples'))
        self.fall_clf = FallClassifier.from_env(cfg)
        self.fusion = FallFusion.from_env(self.fall_clf, cfg) if self.fall_clf else None
        self.fire = FireSmokeDetector.from_env(cfg)
        self.child = ChildSafety.from_env(cfg)
        self.illness = IllnessMonitor.from_env(cfg)
        self.routine = RoutineMonitor.from_env(self.ev_dir, cfg)
        self.pet_log = ActivityLog('pet', self.ev_dir, zone_keys=('pet_zones', 'cat_zones'))
        self.person_log = ActivityLog('person', self.ev_dir,
                                      zone_keys=('bathroom_zones', 'bedroom_zones', 'rest_zones'),
                                      morning_from=self.routine.day_start if self.routine else 4)
        self.clips = None if args.no_clips else ClipRecorder(
            os.path.join(self.ev_dir, 'clips'), pre_sec=float(cfg('CLIP_PRE_SEC', 10)),
            post_sec=float(cfg('CLIP_POST_SEC', 15)), width=int(cfg('CLIP_WIDTH', 640)))

        # ---------- 常驻服务 ----------
        self.frames, self.audio = None, None
        self.stream_down, self.disk = False, {}
        self.start_frames()
        self.start_audio()

    # ================= 服务 =================
    def start_frames(self):
        clips = self.clips
        f = FrameSource(self.args.source, retrieve_fps=5.0,
                        on_frame=(lambda fr, ts: clips.push(fr, ts)) if clips else None)
        f.start()
        self.frames = f

    def start_audio(self):
        if self.args.no_audio:
            return
        a = AudioListener.from_env(cfg)
        if a:
            a.start()
        self.audio = a

    # ================= 信号 → 研判 =================
    def evidence_from(self, frame):
        """返回 evidence(tag, level)：快照在 io 线程写；值守级以上才录像"""
        def ev(tag, lvl):
            if frame is None:
                return None, None
            path = os.path.join(self.ev_dir, 'snap_%s_%s.jpg' % (tag, time.strftime('%Y%m%d_%H%M%S')))
            img = frame.copy()
            self.sched.once('snap', lambda: cv2.imwrite(path, img), lane='io')
            clip = self.clips.trigger(tag) if self.clips and RANK[lvl] >= RANK['GUARD'] else None
            return path, clip
        return ev

    def signal(self, etype, lvl, text, subject=None, pt=None):
        c = self.ctx
        cell = cell_of(pt, c.W, c.H) if pt else None
        return self.incidents.signal(etype, lvl, text, subject=subject, cell=cell,
                                     evidence=self.evidence_from(c.frame))

    def in_rest(self, pt):
        c = self.ctx
        return c.zones_valid and any(in_poly(pt, z['poly']) for z in c.zones.get('rest_zones', []))

    # ================= 主人指令 / 确认 =================
    def on_command(self, text):
        """“状态 / 看看”这类需要画面和调度的指令"""
        c = self.ctx
        if text == '状态':
            return '模式：%s；警觉：%s；未结事件 %d 件；画面%s%s。' % (
                MODE_CN.get(c.mode), c.level, len(self.incidents.open),
                '正常' if self.frames and self.frames.stale_for() < 10 else '中断',
                '' if self.view.aligned else '，镜头偏离标定位置')
        frame, _, _ = self.frames.latest()
        if frame is None:
            return '暂时没有画面。'
        path = os.path.join(self.ev_dir, 'snap_look_%s.jpg' % time.strftime('%Y%m%d_%H%M%S'))
        img = frame.copy()
        self.sched.once('look', lambda: cv2.imwrite(path, img), lane='io')
        return '已抓拍：%s' % path

    def on_ack(self, inc, kind):
        """主人确认 → 给跌倒样本打标签（误报 0 / 属实 1）"""
        if kind in ('false', 'true') and inc.family == 'person_down':
            n = self.samples.label(inc.id, 1 if kind == 'true' else 0)
            if n:
                print('[samples] 事件 %s 标为%s' % (inc.id, '属实' if kind == 'true' else '误报'))

    # ================= 运行 =================
    def run(self):
        for t in duties.roster(self):
            self.sched.add(t)
        for name, at, fn in duties.DAILY:
            self.sched.daily(name, at(), partial(fn, self))
        self.butler.start_command_server()

        print('[butler] 值班表：%s' % [t.name for t in self.sched.tasks])
        print('[butler] 能力：姿态=%s 跌倒模型=%s 作息=%s 火焰=%s 声音=%s 儿童=%s 存证=%s 云台跟踪=%s 跟踪器=%s 区域=%s'
              % (bool(self.pose), self.fusion.mode if self.fusion else False, bool(self.routine),
                 bool(self.fire), bool(self.audio), bool(self.child), bool(self.clips),
                 self.ptz.enabled, type(self.tracker).__name__, [k for k, v in self.zones.items() if v]))
        self.butler.notify('INFO', '%s上线，当前模式：%s。有事我叫你。'
                           % (self.butler.name, MODE_CN[self.butler.mode]), cooldown=0)
        try:
            while True:
                self.sched.tick(self.ctx)
                time.sleep(min(self.sched.next_wakeup(self.ctx), 0.1))
        except KeyboardInterrupt:
            print('[butler] 收到退出指令，正在保存…')
        finally:
            self.shutdown()

    def shutdown(self):
        duties.records.save(self, self.ctx)
        self.samples.flush(time.time(), force=True)
        if self.clips:
            self.clips.flush()
        if self.audio:
            self.audio.stop()
        if self.frames:
            self.frames.stop()
        self.sched.shutdown()


def add_run_args(ap):
    ap.add_argument('--source', default=cfg('RTSP_URL'), help='RTSP 地址或录像文件')
    ap.add_argument('--det', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n.onnx'))
    ap.add_argument('--pose', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n-pose.onnx'))
    ap.add_argument('--zones', default='zones.json')
    ap.add_argument('--events-dir', '--heatmap-dir', dest='events_dir', default=cfg('EVENTS_DIR', 'events'))
    ap.add_argument('--follow', action='store_true', help='无人值守时云台跟踪入侵者')
    ap.add_argument('--no-audio', action='store_true', help='关闭声音事件')
    ap.add_argument('--no-clips', action='store_true', help='关闭视频存证')
    ap.add_argument('--no-display', action='store_true', help='保留兼容，执事本身不开窗口')
    return ap
