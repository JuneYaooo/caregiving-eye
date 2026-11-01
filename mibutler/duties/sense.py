# -*- coding: utf-8 -*-
"""看与听：看门狗、动静、检测跟踪、姿态与跌倒、声音事件

每个函数签名都是 fn(app, c)：app 是组装好的执事（模型、服务、组件），c 是本次运行的共享状态。
"""
import time
from mibutler.butler import AWAY, PET, NIGHT
from mibutler.brain.incidents import RANK
from mibutler.vision.yolo import PERSON, CAT, DOG
from mibutler.vision.tracker import SimpleTracker
from mibutler.abilities.fall import FALL_CFG
from mibutler.config import cfg

PET_MIN_SCORE = 0.45


def watchdog(app, c):
    """断流、拉流线程挂掉、声音线程卡住时自动恢复"""
    f = app.frames
    stale = f.stale_for() if f else float('inf')
    if f is None or not f.is_alive():
        app.start_frames()
    down_sec = float(cfg('STREAM_DOWN_SEC', 120))
    if stale > down_sec and time.time() - c.started > down_sec and not app.stream_down:
        app.stream_down = True
        app.signal('stream_down', 'GUARD', '画面已中断 %d 秒，正在自动重连（已重连 %d 次）。'
                   % (min(stale, 9e5), f.reconnects if f else 0))
    elif stale < 10 and app.stream_down:
        app.stream_down = False
        app.butler.notify('INFO', '画面已恢复。', event_type='stream_up', cooldown=0)
    a = app.audio
    if a is not None and not app.args.no_audio:
        if not a.is_alive() and not a.disabled:
            app.start_audio()
        elif a.is_alive() and a.kick_if_stalled(60):
            print('[audio] 60 秒没收到声音数据，重连')


def motion(app, c):
    """帧差判断动静，更新警觉等级和区域是否可用"""
    frame, ts, seq = app.frames.latest()
    if frame is None or seq == c.motion_seq:
        return
    c.motion_seq = seq
    app.gate.update(frame, ts)
    now = time.time()
    c.level = app.level.update(now, now - app.gate.last_motion < 10, bool(c.persons),
                               app.incidents.highest_open() >= RANK['GUARD'])
    c.zones_valid = app.ptz.zones_valid and app.view.aligned


def detect(app, c):
    """人 / 猫狗检测 + 跟踪，顺便更新值班模式"""
    frame, ts, seq = app.frames.latest()
    if frame is None or seq == c.frame_seq:
        return
    c.frame, c.frame_ts, c.frame_seq = frame, ts, seq
    c.H, c.W = frame.shape[:2]
    dets = [d for d in app.det(frame)
            if (d['cls'] == PERSON and d['score'] > FALL_CFG['min_score'])
            or (d['cls'] in (CAT, DOG) and d['score'] > PET_MIN_SCORE)]
    c.tracks = app.tracker.update(dets, ts)
    c.persons = [t for t in c.tracks if t.cls == PERSON]
    c.pets = [t for t in c.tracks if t.cls in (CAT, DOG)]
    c.mode = app.butler.update_presence(bool(c.persons), only_pets=bool(c.pets) and not c.persons)
    if c.persons:
        c.last_person_ts = c.person_seen_ts = ts
    c.det_seq += 1


def pose(app, c):
    """有人才跑姿态：站 / 坐 / 躺、跌倒、倒地久卧；骨架模型打分；跌倒样本缓存"""
    pose_dets = app.pose(c.frame, 0.3) if app.pose else []
    c.kpts, c.posture = {}, {}
    probs = {}
    for t in c.persons:
        best, bi = None, 0.2
        for p in pose_dets:
            i = SimpleTracker._iou(t.bbox, p['bbox'])
            if i > bi:
                best, bi = p, i
        moved = app.mobility.update(t.tid, t.center(), c.W)
        k = best.get('kpts') if best else None
        evt = app.posture.update(t.tid, k, t.bbox, c.frame_ts)
        c.kpts[t.tid], c.posture[t.tid] = k, app.posture.state(t.tid)
        if not moved:
            continue    # 家具化：没自己动过的人形不参与跌倒 / 久卧判断，也不喂骨架分类器
        c.centers[t.tid] = t.center()
        app.samples.push(t.tid, c.frame_ts, k)
        if app.fall_clf:
            probs[t.tid] = app.fall_clf.update(t.tid, c.frame_ts, k)
        if evt == 'fall':
            if app.fusion:
                fall_verdicts(app, c, app.fusion.rule_fired(t.tid, c.frame_ts))
            else:
                fall_verdicts(app, c, [(t.tid, 'rule', 0.0)])
        elif evt == 'lying_long' and not app.in_rest(t.center()):
            # 在床、沙发等休息区躺着不算倒地久卧；那里的异常交给突发不适多信号判断
            app.signal('lying_long', 'CRITICAL', '有人倒地超过 %g 秒未起身（#%d），请查看。'
                       % (FALL_CFG['lying_min_secs'], t.tid), subject=t.tid, pt=t.center())
    if app.fusion:
        fall_verdicts(app, c, app.fusion.step(c.frame_ts, probs, c.posture))

    alive = set(app.tracker.tracks)
    app.posture.forget(alive)
    app.mobility.forget(alive)
    app.samples.forget(alive)
    pending = set()
    if app.fall_clf:
        app.fall_clf.forget(alive)
        app.fusion.forget(alive)
        pending = set(app.fusion.pending)
    c.centers = {tid: p for tid, p in c.centers.items() if tid in alive or tid in pending}


def fall_verdicts(app, c, verdicts):
    """跌倒结论 → 信号；同时登记跌倒样本"""
    for tid, kind, p in verdicts:
        c.fall_ts[tid] = time.time()
        pt = c.centers.get(tid)
        if kind == 'confirmed':
            inc = app.signal('fall', 'CRITICAL', '判断有人跌倒（#%d，姿态规则与骨架模型都判断为跌倒，%.0f%%），'
                             '已抓拍并录像。' % (tid, p * 100), subject=tid, pt=pt)
        elif kind == 'rule':
            inc = app.signal('fall', 'CRITICAL', '判断有人跌倒（#%d%s），已抓拍并录像。'
                             % (tid, '，骨架模型 %.0f%%' % (p * 100) if p else ''), subject=tid, pt=pt)
        elif kind == 'rule_only':
            inc = app.signal('fall_suspect', 'GUARD', '疑似跌倒（#%d）：姿态从站立突然变为倒地，骨架模型未确认。'
                             '已抓拍并录像，一直没起身会升级为急报。' % tid, subject=tid, pt=pt)
        else:
            inc = app.signal('fall_suspect', 'GUARD', '疑似跌倒（#%d）：骨架模型判断刚才的动作像跌倒（%.0f%%）。'
                             '已抓拍并录像。' % (tid, p * 100), subject=tid, pt=pt)
        if inc is not None:
            app.samples.mark(tid, c.frame_ts, inc.id, 'fall' if kind in ('confirmed', 'rule') else 'fall_suspect')


def fall_pending(app, c):
    """规则报了跌倒但人随后离开画面：等待时间到了也要给出结论"""
    fall_verdicts(app, c, app.fusion.step(time.time(), {}, {}))


def audio_events(app, c):
    """取出声音事件；呼喊呻吟不单独提醒，留给突发不适当佐证"""
    a = app.audio
    n = 0
    while a and not a.events.empty() and n < 50:
        ts, g, score, top = a.events.get_nowait()
        n += 1
        if g == 'smoke_alarm':
            app.signal('fire_alarm_sound', 'CRITICAL', '听到烟雾 / 火警报警器的声音（%.2f）！' % score)
        elif g == 'glass' and c.mode in (AWAY, PET, NIGHT):
            app.signal('glass_break', 'GUARD', '听到玻璃碎裂或撞击声（%.2f）。' % score)
        elif g == 'baby_cry':
            app.signal('baby_cry', 'GUARD', '听到婴儿哭声。')
        elif g == 'pet_distress' and c.mode in (PET, AWAY):
            app.signal('pet_distress', 'PET', '听到宠物嘶叫或哀鸣。')
