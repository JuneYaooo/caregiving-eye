# -*- coding: utf-8 -*-
"""米执事主程序：大脑 + 值班表

结构（详见 docs/brain.md）：
  常驻服务   拉流线程（只留最新帧）、声音线程（安静时不跑模型）
  警觉等级   idle / watch / alert，决定检测频率，没人没动静时自动降速省电
  值班表     main() 里“值班表”一段，每一行就是一项任务：多久跑一次、什么模式跑、什么条件跑、
             谁优先；模型推理串行，写文件 / 发消息 / 转云台走 io 线程并行
  研判层     各能力只上报信号，由 IncidentManager 合并、升级、催办、结案、学习误报
  保养       磁盘清理、日志轮转、心跳 status.json、断流看门狗、每日日报

用法:
  python care_v2.py --source rtsp://127.0.0.1:8554/living
  python care_v2.py --source rtsp://... --follow          # 无人时云台跟踪
可选能力缺模型或依赖时自动跳过，启动日志会列出实际启用了哪些。
"""
import argparse, json, os, time
import numpy as np
import cv2
from common_config import cfg
from simple_tracker import SimpleTracker
from butler import Butler, AWAY, HOME, PET, NIGHT, MODE_CN
from brain.runtime import Scheduler, Task
from brain.frames import FrameSource
from brain.gate import MotionGate, AlertLevel, IDLE, WATCH, ALERT
from brain.incidents import IncidentManager, RANK
from brain.ptz import PtzController
from brain import upkeep
from abilities.clips import ClipRecorder
from abilities.fire import FireSmokeDetector
from abilities.audio import AudioListener
from abilities.child import ChildSafety
from abilities.illness import IllnessMonitor
from abilities.pet import ActivityLog
from abilities.tracking import make_tracker
from abilities.fall_gcn import FallClassifier, FallFusion
from abilities.routine import RoutineMonitor

PERSON, CAT, DOG = 0, 15, 16
CLASS_NAMES = {PERSON: 'person', CAT: 'cat', DOG: 'dog'}

FALL_CFG = {
    'stand_aspect_max': 0.9, 'lying_aspect_min': 1.1,
    'fall_transition_secs': 2.5, 'min_score': 0.4,
    'lying_min_secs': 30, 'lying_realert': 300,
}
CAT_CFG = {'min_score': 0.45, 'cooldown': 120}
ABSENCE_MIN = float(cfg('ABSENCE_MIN', 480))       # 长时间无人出现提醒(分钟)
ZONE_DWELL_MIN = float(cfg('ZONE_DWELL_MIN', 20))  # 卫生间久滞(分钟)

# 各警觉等级下的检测间隔（秒）
DETECT_EVERY = {IDLE: float(cfg('DETECT_IDLE_SEC', 1.5)),
                WATCH: 1 / float(cfg('DETECT_FPS', 3)),
                ALERT: 1 / float(cfg('DETECT_ALERT_FPS', 5))}
FIRE_EVERY = {IDLE: 5.0, WATCH: 3.0, ALERT: 2.0}


def in_poly(pt, poly):
    return cv2.pointPolygonTest(np.array(poly, np.float32), pt, False) >= 0


class PostureSM:
    """姿态状态机: 关键点几何 + 迟滞; 挂在 track_id 上"""
    HYS = 2  # 新状态需连续2次判定才切换

    def __init__(self):
        self.sm = {}

    def _geom(self, kpts, bbox):
        """返回 (angle_deg, torso_len, aspect)"""
        x1, y1, x2, y2 = bbox
        aspect = (x2 - x1) / max(y2 - y1, 1e-6)
        if not kpts:
            return None, None, aspect
        def mid(i, j):
            a, b = kpts[i], kpts[j]
            if a[2] > 0.3 and b[2] > 0.3:
                return (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
            return None
        ms = mid(5, 6)   # 双肩中点
        mh = mid(11, 12)  # 双髋中点
        if not ms or not mh:
            return None, None, aspect
        vx, vy = mh[0] - ms[0], mh[1] - ms[1]
        angle = abs(np.degrees(np.arctan2(vx, vy)))  # 0=竖直, 90=水平
        return angle, np.hypot(vx, vy), aspect

    def _state_of(self, angle, aspect):
        if angle is None:
            return 'lie' if aspect > FALL_CFG['lying_aspect_min'] else \
                   ('stand' if aspect < FALL_CFG['stand_aspect_max'] else 'unknown')
        if angle < 35:
            return 'stand'
        if angle > 65:
            return 'lie'
        return 'sit'

    def update(self, tid, kpts, bbox, now):
        st = self.sm.setdefault(tid, {'state': 'unknown', 'cand': None, 'cand_n': 0,
                                      'last_stand_ts': now, 'lying_since': None,
                                      'last_lying_alert': 0, 'fall_alerted': False})
        if st['state'] == 'stand':
            st['last_stand_ts'] = now   # 持续站立时也要刷新，否则站久了的人跌倒永远判不出来
        angle, tlen, aspect = self._geom(kpts, bbox)
        new = self._state_of(angle, aspect)
        if new == st['state'] or new == 'unknown':
            st['cand'], st['cand_n'] = None, 0
        else:
            st['cand_n'] = st['cand_n'] + 1 if st['cand'] == new else 1
            st['cand'] = new
            if st['cand_n'] >= self.HYS:
                prev, st['state'] = st['state'], new
                st['cand'], st['cand_n'] = None, 0
                if new == 'stand':
                    st['last_stand_ts'] = now
                    st['fall_alerted'] = False
                if new == 'lie':
                    st['lying_since'] = now
                    # 跌倒判定: 近期站立 → 突然倒地
                    if prev == 'stand' and now - st['last_stand_ts'] < FALL_CFG['fall_transition_secs'] \
                            and not st['fall_alerted']:
                        st['fall_alerted'] = True
                        return 'fall'
                else:
                    st['lying_since'] = None
        # 倒地久卧
        if st['state'] == 'lie' and st['lying_since'] \
                and now - st['lying_since'] > FALL_CFG['lying_min_secs'] \
                and now - st['last_lying_alert'] > FALL_CFG['lying_realert']:
            st['last_lying_alert'] = now
            return 'lying_long'
        return None


ZONE_KEYS = ('cat_zones', 'pet_zones', 'bathroom_zones', 'bedroom_zones',
             'rest_zones', 'danger_zones')


def load_zones(path):
    if not os.path.exists(path):
        return {}
    z = json.load(open(path, encoding='utf-8'))
    return {k: z.get(k, []) for k in ZONE_KEYS}


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


def cell_of(pt, W, H):
    """把画面分成 6×4 格，误报学习按格子记"""
    return 'c%d_%d' % (min(int(pt[0] / W * 6), 5), min(int(pt[1] / H * 4), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default=cfg('RTSP_URL'))
    ap.add_argument('--det', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n.onnx'))
    ap.add_argument('--pose', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n-pose.onnx'))
    ap.add_argument('--zones', default='zones.json')
    ap.add_argument('--events-dir', '--heatmap-dir', dest='events_dir',
                    default=cfg('EVENTS_DIR', 'events'))
    ap.add_argument('--follow', action='store_true', help='无人值守时云台跟踪入侵者')
    ap.add_argument('--no-audio', action='store_true', help='关闭声音事件')
    ap.add_argument('--no-clips', action='store_true', help='关闭视频存证')
    ap.add_argument('--no-display', action='store_true', help='保留兼容，执事本身不开窗口')
    ap.add_argument('--test-image', default='')
    args = ap.parse_args()

    from care_pipeline import YoloOnnx
    det_model = YoloOnnx(args.det)
    if args.test_image:
        for d in det_model(cv2.imread(args.test_image)):
            print('det', CLASS_NAMES.get(d['cls'], d['cls']), round(d['score'], 2))
        return
    pose_model = YoloOnnx(args.pose) if os.path.exists(args.pose) else None

    ev_dir = args.events_dir
    os.makedirs(ev_dir, exist_ok=True)
    zones = load_zones(args.zones)
    ctx = Ctx(zones)

    # ---------- 组件 ----------
    butler = Butler(events_path=os.path.join(ev_dir, 'alerts.jsonl'))
    incidents = IncidentManager(butler, ev_dir,
                                remind_sec=float(cfg('REMIND_SEC', 180)),
                                max_reminds=int(cfg('MAX_REMINDS', 5)))
    butler.incidents = incidents
    sched = Scheduler(io_workers=2, budget=float(cfg('TICK_BUDGET', 0.6)))
    tracker = make_tracker(cfg('TRACKER', 'iou'), fps=3, max_misses=DETECT_EVERY[IDLE] * 3)
    psm = PostureSM()
    # 家具化降级: 从未自主移动过的人形 track (雕像/摆件/红外噪声) 没有跌倒/久卧/入侵告警资格
    MOBILE_TH = float(cfg('MOBILE_TH', 0.04))  # 位移超过画面宽度的该比例视为"动过"
    mobile_seen = {}  # tid -> {'ax','ay','moved'}
    gate, level = MotionGate(), AlertLevel(hold=float(cfg('CALM_DOWN_SEC', 30)))
    ptz = PtzController(enabled=args.follow)
    fire = FireSmokeDetector.from_env(cfg)
    child = ChildSafety.from_env(cfg)
    illness = IllnessMonitor.from_env(cfg)
    fall_clf = FallClassifier.from_env(cfg)
    fusion = FallFusion.from_env(fall_clf, cfg) if fall_clf else None
    routine = RoutineMonitor.from_env(ev_dir, cfg)
    pet_log = ActivityLog('pet', ev_dir, zone_keys=('pet_zones', 'cat_zones'))
    person_log = ActivityLog('person', ev_dir,
                             zone_keys=('bathroom_zones', 'bedroom_zones', 'rest_zones'),
                             morning_from=routine.day_start if routine else 4)
    clips = None if args.no_clips else ClipRecorder(
        os.path.join(ev_dir, 'clips'), pre_sec=float(cfg('CLIP_PRE_SEC', 10)),
        post_sec=float(cfg('CLIP_POST_SEC', 15)), width=int(cfg('CLIP_WIDTH', 640)))

    # ---------- 常驻服务 ----------
    svc = {'frames': None, 'audio': None, 'stream_down': False, 'disk': {}}

    def start_frames():
        f = FrameSource(args.source, retrieve_fps=5.0,
                        on_frame=(lambda fr, ts: clips.push(fr, ts)) if clips else None)
        f.start()
        svc['frames'] = f

    def start_audio():
        if args.no_audio:
            return
        a = AudioListener.from_env(cfg)
        if a:
            a.start()
        svc['audio'] = a

    start_frames()
    start_audio()

    # ---------- 工具 ----------
    def evidence_from(frame):
        """返回 evidence(tag, level)：快照在 io 线程写；值守级以上才录像"""
        def ev(tag, lvl):
            if frame is None:
                return None, None
            path = os.path.join(ev_dir, 'snap_%s_%s.jpg' % (tag, time.strftime('%Y%m%d_%H%M%S')))
            img = frame.copy()
            sched.once('snap', lambda: cv2.imwrite(path, img), lane='io')
            clip = clips.trigger(tag) if clips and RANK[lvl] >= RANK['GUARD'] else None
            return path, clip
        return ev

    def signal(etype, lvl, text, subject=None, pt=None):
        cell = cell_of(pt, ctx.W, ctx.H) if pt else None
        return incidents.signal(etype, lvl, text, subject=subject, cell=cell,
                                evidence=evidence_from(ctx.frame))

    # ================= 值班表 =================
    # 优先级：0 看门 / 1 看见 / 2 生命财产 / 3 照护研判 / 4 区域规则 / 6+ 统计（过载时可让路）

    def t_watchdog(c):
        f = svc['frames']
        stale = f.stale_for() if f else float('inf')
        if f is None or not f.is_alive():
            start_frames()
        down_sec = float(cfg('STREAM_DOWN_SEC', 120))
        if stale > down_sec and time.time() - c.started > down_sec and not svc['stream_down']:
            svc['stream_down'] = True
            signal('stream_down', 'GUARD', '画面已中断 %d 秒，正在自动重连（已重连 %d 次）。'
                   % (min(stale, 9e5), f.reconnects if f else 0))
        elif stale < 10 and svc['stream_down']:
            svc['stream_down'] = False
            butler.notify('INFO', '画面已恢复。', event_type='stream_up', cooldown=0)
        a = svc['audio']
        if a is not None and not args.no_audio:
            if not a.is_alive() and not a.disabled:
                start_audio()
            elif a.is_alive() and a.kick_if_stalled(60):
                print('[audio] 60 秒没收到声音数据，重连')

    def t_motion(c):
        frame, ts, seq = svc['frames'].latest()
        if frame is None or seq == c.motion_seq:
            return
        c.motion_seq = seq
        gate.update(frame, ts)
        now = time.time()
        c.level = level.update(now, now - gate.last_motion < 10, bool(c.persons),
                               incidents.highest_open() >= RANK['GUARD'])
        c.zones_valid = ptz.zones_valid

    def t_detect(c):
        frame, ts, seq = svc['frames'].latest()
        if frame is None or seq == c.frame_seq:
            return
        c.frame, c.frame_ts, c.frame_seq = frame, ts, seq
        c.H, c.W = frame.shape[:2]
        dets = [d for d in det_model(frame)
                if (d['cls'] == PERSON and d['score'] > FALL_CFG['min_score'])
                or (d['cls'] in (CAT, DOG) and d['score'] > CAT_CFG['min_score'])]
        c.tracks = tracker.update(dets, ts)
        c.persons = [t for t in c.tracks if t.cls == PERSON]
        c.pets = [t for t in c.tracks if t.cls in (CAT, DOG)]
        c.mode = butler.update_presence(bool(c.persons),
                                        only_pets=bool(c.pets) and not c.persons)
        if c.persons:
            c.last_person_ts = c.person_seen_ts = ts
        c.det_seq += 1

    def in_rest(pt):
        return ctx.zones_valid and any(in_poly(pt, z['poly']) for z in ctx.zones.get('rest_zones', []))

    def fall_verdicts(verdicts):
        """姿态规则与骨架模型并联研判的结论 → 信号"""
        for tid, kind, p in verdicts:
            ctx.fall_ts[tid] = time.time()
            pt = ctx.centers.get(tid)
            if kind == 'confirmed':
                signal('fall', 'CRITICAL', '判断有人跌倒（#%d，姿态规则与骨架模型都判断为跌倒，%.0f%%），'
                       '已抓拍并录像。' % (tid, p * 100), subject=tid, pt=pt)
            elif kind == 'rule':
                signal('fall', 'CRITICAL', '判断有人跌倒（#%d%s），已抓拍并录像。'
                       % (tid, '，骨架模型 %.0f%%' % (p * 100) if p else ''), subject=tid, pt=pt)
            elif kind == 'rule_only':
                signal('fall_suspect', 'GUARD', '疑似跌倒（#%d）：姿态从站立突然变为倒地，骨架模型未确认。'
                       '已抓拍并录像，一直没起身会升级为急报。' % tid, subject=tid, pt=pt)
            else:
                signal('fall_suspect', 'GUARD', '疑似跌倒（#%d）：骨架模型判断刚才的动作像跌倒（%.0f%%）。'
                       '已抓拍并录像。' % (tid, p * 100), subject=tid, pt=pt)

    def t_pose(c):
        pose_dets = pose_model(c.frame, 0.3) if pose_model else []
        c.kpts, c.posture = {}, {}
        probs = {}
        for t in c.persons:
            best, bi = None, 0.2
            for p in pose_dets:
                i = SimpleTracker._iou(t.bbox, p['bbox'])
                if i > bi:
                    best, bi = p, i
            # 自主移动判定: 锚点位移超阈值才算"动过", 一票获得告警资格
            cx, cy = t.center()
            m = mobile_seen.setdefault(t.tid, {'ax': cx, 'ay': cy, 'moved': False})
            if abs(cx - m['ax']) + abs(cy - m['ay']) > MOBILE_TH * c.W:
                m['ax'], m['ay'], m['moved'] = cx, cy, True
            k = best.get('kpts') if best else None
            evt = psm.update(t.tid, k, t.bbox, c.frame_ts)
            c.kpts[t.tid], c.posture[t.tid] = k, psm.sm[t.tid]['state']
            if not m['moved']:
                continue  # 家具化: 没动过就不具备跌倒/久卧告警资格, 也不喂骨架分类器
            c.centers[t.tid] = t.center()
            if fall_clf:
                probs[t.tid] = fall_clf.update(t.tid, c.frame_ts, k)
            if evt == 'fall':
                if fusion:
                    fall_verdicts(fusion.rule_fired(t.tid, c.frame_ts))
                else:
                    c.fall_ts[t.tid] = time.time()
                    signal('fall', 'CRITICAL', '判断有人跌倒（#%d），已抓拍并录像。' % t.tid,
                           subject=t.tid, pt=t.center())
            elif evt == 'lying_long' and not in_rest(t.center()):
                # 在床、沙发等休息区躺着不算倒地久卧；那里的异常交给突发不适多信号判断
                signal('lying_long', 'CRITICAL', '有人倒地超过 %g 秒未起身（#%d），请查看。'
                       % (FALL_CFG['lying_min_secs'], t.tid), subject=t.tid, pt=t.center())
        if fusion:
            fall_verdicts(fusion.step(c.frame_ts, probs, c.posture))
        for tid in list(psm.sm):
            if tid not in tracker.tracks:
                psm.sm.pop(tid)
        for tid in list(mobile_seen):
            if tid not in tracker.tracks:
                mobile_seen.pop(tid)
        if fall_clf:
            alive = set(tracker.tracks)
            fall_clf.forget(alive)
            fusion.forget(alive)
            for tid in list(c.centers):
                if tid not in alive and tid not in fusion.pending:
                    c.centers.pop(tid)
        else:
            c.centers = {tid: p for tid, p in c.centers.items() if tid in tracker.tracks}

    def t_fall_pending(c):
        """规则报了跌倒但人随后从画面里消失：等待时间到了也要给出结论"""
        fall_verdicts(fusion.step(time.time(), {}, {}))

    def t_routine(c):
        with person_log._lock:
            stats = dict(person_log.stats)
        for lvl, et, text in routine.check(stats, time.time(), c.person_seen_ts):
            signal(et, lvl, text)

    def t_fire(c):
        frame, ts, _ = svc['frames'].latest()
        if frame is None:
            return
        r = fire.update(frame, ts)
        if not r:
            return
        what = '、'.join(sorted({'fire': '明火', 'smoke': '烟雾'}.get(l, l) for l in r['labels']))
        a = svc['audio']
        heard = bool(a and a.heard('smoke_alarm', within=60))
        if r['level'] == 'confirmed' or heard:
            signal('fire', 'CRITICAL', '画面中持续出现%s%s，请立即确认！'
                   % (what, '，同时听到报警器声' if heard else ''))
        else:
            signal('fire_suspect', 'GUARD', '疑似出现%s（置信度 %.2f），正在加密观察。' % (what, r['score']))

    def t_audio(c):
        a = svc['audio']
        n = 0
        while a and not a.events.empty() and n < 50:
            ts, g, score, top = a.events.get_nowait()
            n += 1
            if g == 'smoke_alarm':
                signal('fire_alarm_sound', 'CRITICAL', '听到烟雾 / 火警报警器的声音（%.2f）！' % score)
            elif g == 'glass' and c.mode in (AWAY, PET, NIGHT):
                signal('glass_break', 'GUARD', '听到玻璃碎裂或撞击声（%.2f）。' % score)
            elif g == 'baby_cry':
                signal('baby_cry', 'GUARD', '听到婴儿哭声。')
            elif g == 'pet_distress' and c.mode in (PET, AWAY):
                signal('pet_distress', 'PET', '听到宠物嘶叫或哀鸣。')
            # distress（呼喊呻吟）不单独提醒，作为突发不适的佐证信号

    def t_intrusion(c):
        for t in c.persons:
            if t.hits < 8:
                continue
            if not mobile_seen.get(t.tid, {}).get('moved'):
                continue  # 家具化: 静止摆件不算入侵者
            signal('intrusion', 'GUARD', '家中无人，但检测到移动的人形（#%d），已抓拍并录像。'
                   '若是您本人，回复"是我"。' % t.tid, pt=t.center())
            dx = (t.center()[0] - c.W / 2) / c.W
            if ptz.can_nudge() and abs(dx) >= 0.15:
                ptz.request()     # 先占位，转完之前不再排新的转动，避免按旧画面连转过头
                sched.once('ptz_follow', lambda d=dx: ptz.nudge(d), lane='io')
            break

    def t_illness(c):
        a = svc['audio']
        distress = bool(a and a.heard('distress', 30))
        for t in c.persons:
            if not mobile_seen.get(t.tid, {}).get('moved'):
                continue  # 家具化: 静止摆件不参与突发不适研判
            r = illness.update(t.tid, c.kpts.get(t.tid), t.bbox, c.posture.get(t.tid, 'unknown'),
                               t.center(), c.zones if c.zones_valid else {}, time.time(), in_poly,
                               fell_recently=time.time() - c.fall_ts.get(t.tid, 0) < 600,
                               heard_distress=distress, is_night=c.mode == NIGHT)
            if r:
                lvl, et, text = r
                signal(et, lvl, text, subject=t.tid, pt=t.center())
        illness.forget({t.tid for t in c.persons})

    def t_child(c):
        info = [(t, c.kpts.get(t.tid), c.posture.get(t.tid, 'unknown')) for t in c.persons]
        for lvl, et, text in child.update(info, c.zones, c.W, c.H, time.time(), in_poly):
            signal(et, lvl, text, pt=None)
        child.forget(set(tracker.tracks))

    def t_zones(c):
        now = time.time()
        for t in c.persons:
            for z in c.zones.get('bathroom_zones', []):
                key = (t.tid, z['name'])
                if in_poly(t.center(), z['poly']):
                    c.zone_enter.setdefault(key, now)
                    if now - c.zone_enter[key] > ZONE_DWELL_MIN * 60:
                        signal('zone_dwell', 'CRITICAL', '在%s已停留超过 %g 分钟。'
                               % (z['name'], ZONE_DWELL_MIN), subject=t.tid, pt=t.center())
                else:
                    c.zone_enter.pop(key, None)
        for key in list(c.zone_enter):
            if key[0] not in tracker.tracks:      # 目标已被跟踪器丢弃
                c.zone_enter.pop(key)
        if c.mode == NIGHT and c.persons and c.zones.get('bedroom_zones'):
            if not any(in_poly(t.center(), z['poly']) for t in c.persons for z in c.zones['bedroom_zones']):
                signal('night_wander', 'GUARD', '夜间检测到有人在卧室外活动。', pt=c.persons[0].center())
        for t in c.pets:
            for z in c.zones.get('cat_zones', []):
                if in_poly(t.center(), z['poly']):
                    signal('cat_zone', 'PET', '%s上了%s！已抓拍。'
                           % ('猫咪' if t.cls == CAT else '狗狗', z['name']), subject=z['name'])

    def t_activity(c):
        now = time.time()
        pet_log.update(c.pets, c.zones, c.W, c.H, now, in_poly)
        person_log.update(c.persons, c.zones, c.W, c.H, now, in_poly)

    def t_absence(c):
        gone = time.time() - c.last_person_ts
        if gone > ABSENCE_MIN * 60:
            signal('absence', 'INFO', '家中已 %.1f 小时未检测到人。' % (gone / 3600))

    def t_pet_unseen(c):
        hrs = float(cfg('PET_UNSEEN_HOURS', 0))
        u = pet_log.unseen_for(time.time())
        if hrs > 0 and u and u > hrs * 3600:
            signal('pet_unseen', 'PET', '已 %.1f 小时没看到宠物，可能躲起来了，可以留意一下。' % (u / 3600))

    def t_base(c):
        frame, _, _ = svc['frames'].latest()
        if frame is not None:
            cv2.imwrite(os.path.join(ev_dir, 'base_%s.jpg' % time.strftime('%Y%m%d')), frame)

    def t_status(c):
        f, a = svc['frames'], svc['audio']
        total = sum(level.time_in.values()) or 1
        upkeep.write_status(os.path.join(ev_dir, 'status.json'), {
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'uptime_h': round((time.time() - c.started) / 3600, 2),
            'mode': c.mode, 'mode_cn': MODE_CN.get(c.mode), 'level': c.level,
            'level_share': {k: round(v / total, 3) for k, v in level.time_in.items()},
            'zones_valid': c.zones_valid,
            'frames': {'connected': bool(f and f.connected), 'stream_fps': round(f.fps, 1) if f else 0,
                       'stale_sec': round(min(f.stale_for(), 9e5), 1) if f else None,
                       'reconnects': f.reconnects if f else 0},
            'audio': {'running': bool(a and a.is_alive()), 'level_db': round(a.level_db, 1) if a else None,
                      'inferences': a.inferences if a else 0},
            'tasks': sched.stats(),
            'open_incidents': incidents.snapshot(),
            'disk': svc['disk'],
        })

    def t_retention(c):
        svc['disk'] = upkeep.retention(ev_dir, keep_days=float(cfg('RETAIN_DAYS', 7)),
                                       max_gb=float(cfg('MAX_EVENTS_GB', 5)))
        for name in ('alerts.jsonl', 'incidents.jsonl'):
            upkeep.rotate_log(os.path.join(ev_dir, name))

    def t_save(c):
        pet_log.save()
        person_log.save()

    def t_ptz_return(c):
        ptz.maybe_return(target_present=bool(c.persons))

    def daily_report():
        from report import daily_report as build
        text = build(events_dir=ev_dir)
        head = '\n'.join(text.splitlines()[:12])
        butler.notify('MORNING', head, event_type='daily_report', cooldown=0)

    has_persons = lambda c: bool(c.persons)
    zones_ok = lambda c: c.zones_valid
    R = [
        #    名称          函数          间隔                     优先级  车道   触发       模式            条件
        Task('watchdog',   t_watchdog,   10,                      0, 'cpu', desc='断流 / 线程挂掉自动恢复'),
        Task('motion',     t_motion,     0.2,                     0, 'cpu', desc='帧差判断动静，决定警觉等级'),
        Task('detect',     t_detect,     DETECT_EVERY,            1, 'cpu', desc='人 / 猫狗检测 + 跟踪'),
        Task('pose',       t_pose,       None,                    1, 'cpu', 'detect', when=has_persons,
             desc='有人才跑姿态：跌倒、久卧'),
        Task('audio',      t_audio,      0.25,                    2, 'cpu', desc='取出声音事件'),
        Task('intrusion',  t_intrusion,  None,                    2, 'cpu', 'detect', (AWAY, PET), has_persons),
        Task('illness',    t_illness,    None,                    3, 'cpu', 'detect', (HOME, NIGHT), has_persons),
        Task('incidents',  lambda c: incidents.tick(), 5,         3, 'cpu', desc='催办与结案'),
        Task('zones',      t_zones,      None,                    4, 'cpu', 'detect', when=zones_ok),
        Task('activity',   t_activity,   None,                    6, 'cpu', 'detect', when=zones_ok,
             desc='活动统计与热力图，过载时让路'),
        Task('absence',    t_absence,    60,                      7, 'cpu'),
        Task('pet_unseen', t_pet_unseen, 300,                     7, 'cpu', modes=(PET,)),
        Task('save',       t_save,       300,                     8, 'io'),
        Task('status',     t_status,     30,                      8, 'io'),
        Task('base_image', t_base,       600,                     9, 'io',
             when=lambda c: c.level == IDLE and not c.tracks),
        Task('retention',  t_retention,  3600,                    9, 'io'),
    ]
    if fire:
        R.append(Task('fire', t_fire, lambda c: 0.5 if fire.hot else FIRE_EVERY[c.level], 2, 'cpu',
                      desc='闲时 5 秒一次，有可疑立刻加密到 0.5 秒'))
    if child:
        R.append(Task('child', t_child, None, 3, 'cpu', 'detect',
                      when=lambda c: bool(c.persons) and c.zones_valid))
    if fusion:
        R.append(Task('fall_pending', t_fall_pending, 1, 1, 'cpu',
                      when=lambda c: bool(fusion.pending) and not c.persons,
                      desc='跌倒待定的人离开画面后照样给出结论'))
    if routine:
        R.append(Task('routine', t_routine, 600, 7, 'cpu', desc='作息偏离：起床晚、活动少、夜里多动'))
    if ptz.enabled:
        R.append(Task('ptz_return', t_ptz_return, 5, 9, 'io', desc='目标离开后云台复位'))
    for t in R:
        sched.add(t)
    sched.daily('daily_report', cfg('REPORT_AT', '21:30'), daily_report)

    # 指令里需要调度的部分
    def on_command(text):
        if text == '状态':
            return '模式：%s；警觉：%s；未结事件 %d 件；画面%s。' % (
                MODE_CN.get(ctx.mode), ctx.level, len(incidents.open),
                '正常' if svc['frames'] and svc['frames'].stale_for() < 10 else '中断')
        frame, _, _ = svc['frames'].latest()
        if frame is None:
            return '暂时没有画面。'
        path = os.path.join(ev_dir, 'snap_look_%s.jpg' % time.strftime('%Y%m%d_%H%M%S'))
        img = frame.copy()
        sched.once('look', lambda: cv2.imwrite(path, img), lane='io')
        return '已抓拍：%s' % path
    butler.on_command = on_command
    butler.start_command_server()

    enabled = [t.name for t in sched.tasks]
    print('[butler] 值班表：%s' % enabled)
    print('[butler] 能力：姿态=%s 跌倒模型=%s 作息=%s 火焰=%s 声音=%s 儿童=%s 存证=%s 云台跟踪=%s 跟踪器=%s 区域=%s'
          % (bool(pose_model), fusion.mode if fusion else False, bool(routine),
             bool(fire), bool(svc['audio']), bool(child), bool(clips),
             ptz.enabled, type(tracker).__name__, [k for k, v in zones.items() if v]))
    butler.notify('INFO', '%s上线，当前模式：%s。有事我叫你。' % (butler.name, MODE_CN[butler.mode]),
                  cooldown=0)

    try:
        while True:
            sched.tick(ctx)
            time.sleep(min(sched.next_wakeup(ctx), 0.1))
    except KeyboardInterrupt:
        print('[butler] 收到退出指令，正在保存…')
    finally:
        t_save(ctx)
        if clips:
            clips.flush()
        if svc['audio']:
            svc['audio'].stop()
        if svc['frames']:
            svc['frames'].stop()
        sched.shutdown()


if __name__ == '__main__':
    main()
