# -*- coding: utf-8 -*-
"""米执事感知核心 v2：按值班模式组合各项能力

  所有模式   火焰 / 烟雾（视觉 + 烟雾报警器声音）、玻璃碎裂声、事件视频存证
  无人值守   入侵检测、云台跟踪（--follow）、长时间无人
  在家照护   跌倒、倒地久卧、突发不适（多信号）、儿童危险、卫生间久滞、夜间离床
  宠物       禁区提醒、活动记录、热力图、宠物嘶叫、久未出现

可选能力缺模型或依赖时自动跳过，启动日志会列出实际启用了哪些。
用法:
  python care_v2.py --source rtsp://127.0.0.1:8554/living --no-display
zones.json 见 zones.example.json
"""
import argparse, json, os, time
import numpy as np
import cv2
from common_config import cfg
from simple_tracker import SimpleTracker
from butler import Butler, AWAY, HOME, PET, NIGHT
from abilities.clips import ClipRecorder
from abilities.fire import FireSmokeDetector
from abilities.audio import AudioListener
from abilities.child import ChildSafety
from abilities.illness import IllnessMonitor
from abilities.pet import ActivityLog
from abilities.tracking import make_tracker

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


AUDIO_TEXT = {
    'smoke_alarm': ('CRITICAL', 'fire_alarm_sound', '听到烟雾 / 火警报警器的声音！'),
    'glass': ('GUARD', 'glass_break', '听到玻璃碎裂或撞击声。'),
    'baby_cry': ('GUARD', 'baby_cry', '听到婴儿哭声。'),
    'pet_distress': ('PET', 'pet_distress', '听到宠物嘶叫或哀鸣。'),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default=cfg('RTSP_URL'))
    ap.add_argument('--det', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n.onnx'))
    ap.add_argument('--pose', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n-pose.onnx'))
    ap.add_argument('--zones', default='zones.json')
    ap.add_argument('--events-dir', '--heatmap-dir', dest='events_dir',
                    default=cfg('EVENTS_DIR', 'events'))
    ap.add_argument('--no-display', action='store_true')
    ap.add_argument('--follow', action='store_true', help='无人值守时云台跟踪入侵者')
    ap.add_argument('--no-audio', action='store_true', help='关闭声音事件')
    ap.add_argument('--no-clips', action='store_true', help='关闭视频存证')
    ap.add_argument('--fps', type=float, default=3.0)
    ap.add_argument('--test-image', default='')
    args = ap.parse_args()

    from care_pipeline import YoloOnnx
    det_model = YoloOnnx(args.det)
    pose_model = YoloOnnx(args.pose) if os.path.exists(args.pose) else None
    if args.test_image:
        img = cv2.imread(args.test_image)
        for d in det_model(img):
            print('det', CLASS_NAMES.get(d['cls'], d['cls']), round(d['score'], 2))
        return

    ev_dir = args.events_dir
    os.makedirs(ev_dir, exist_ok=True)
    butler = Butler(events_path=os.path.join(ev_dir, 'alerts.jsonl'))
    tracker = make_tracker(cfg('TRACKER', 'iou'), fps=args.fps)
    psm = PostureSM()
    zones = load_zones(args.zones)

    # ---- 可选能力 ----
    fire = FireSmokeDetector.from_env(cfg)
    child = ChildSafety.from_env(cfg)
    illness = IllnessMonitor.from_env(cfg)
    pet_log = ActivityLog('pet', ev_dir, zone_keys=('pet_zones', 'cat_zones'))
    person_log = ActivityLog('person', ev_dir,
                             zone_keys=('bathroom_zones', 'bedroom_zones', 'rest_zones'))
    clips = None if args.no_clips else ClipRecorder(
        os.path.join(ev_dir, 'clips'), pre_sec=float(cfg('CLIP_PRE_SEC', 10)),
        post_sec=float(cfg('CLIP_POST_SEC', 15)))

    def on_audio(group, score, top):
        if group in AUDIO_TEXT:
            level, et, text = AUDIO_TEXT[group]
            if group == 'glass' and butler.mode not in (AWAY, PET, NIGHT):
                return
            if group == 'pet_distress' and butler.mode not in (PET, AWAY):
                return
            clip = clips.trigger(et) if clips else None
            butler.notify(level, '%s（置信度 %.2f）' % (text, score), event_type=et, clip=clip)

    audio = None if args.no_audio else AudioListener.from_env(cfg, on_event=on_audio)
    if audio:
        audio.start()
    pet_unseen_h = float(cfg('PET_UNSEEN_HOURS', 0))
    abilities = ['检测', '姿态' if pose_model else '姿态(未加载)',
                 '火焰烟雾' if fire else '火焰烟雾(无模型)',
                 '声音' if audio else '声音(未启用)',
                 '儿童' if child else '儿童(HAS_CHILD=0)',
                 '存证' if clips else '存证(关)', type(tracker).__name__]

    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print('ERROR: cannot open source', args.source)
        return
    n_warm, t0 = 0, time.time()
    while n_warm < 40 and time.time() - t0 < 20:   # 等关键帧
        ok, _ = cap.read()
        if ok:
            n_warm += 1
    print('[care_v2] warm=%d frames, zones=%s, abilities=%s'
          % (n_warm, [k for k, v in zones.items() if v], abilities))
    butler.notify('INFO', '%s上线，当前模式：%s。有事我叫你。' % (butler.name, butler.mode),
                  cooldown=0)

    last_ana = last_status = last_follow = last_absence = last_fire = 0
    last_save, last_base = time.time(), 0
    fall_ts = {}                     # tid -> 最近一次跌倒时间
    last_person_ts = time.time()     # 最近一次看到人
    frame_id = 0

    def snap(frame, tag):
        p = os.path.join(ev_dir, 'snap_%s_%s.jpg' % (tag, time.strftime('%Y%m%d_%H%M%S')))
        cv2.imwrite(p, frame)
        return p

    def evidence(frame, tag):
        return snap(frame, tag), (clips.trigger(tag) if clips else None)

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.5)
                cap.release()
                cap = cv2.VideoCapture(args.source)
                continue
            frame_id += 1
            now = time.time()
            if clips:
                clips.push(frame, now)
            if now - last_ana < 1.0 / args.fps:
                continue
            last_ana = now
            H, W = frame.shape[:2]

            dets = [d for d in det_model(frame)
                    if (d['cls'] == PERSON and d['score'] > FALL_CFG['min_score'])
                    or (d['cls'] in (CAT, DOG) and d['score'] > CAT_CFG['min_score'])]
            pose_dets = pose_model(frame, 0.3) if pose_model else []
            tracks = tracker.update(dets, now)
            person_tracks = [t for t in tracks if t.cls == PERSON]
            pet_tracks = [t for t in tracks if t.cls in (CAT, DOG)]
            butler.update_presence(bool(person_tracks),
                                   only_pets=bool(pet_tracks) and not person_tracks)
            if person_tracks:
                last_person_ts = now

            def nearest_pose(bbox):
                best, bi = None, 0.2
                for p in pose_dets:
                    i = SimpleTracker._iou(bbox, p['bbox'])
                    if i > bi:
                        best, bi = p, i
                return best

            # ---- 火焰 / 烟雾：所有模式，约 1 次/秒 ----
            if fire and now - last_fire >= 1.0:
                last_fire = now
                r = fire.update(frame, now)
                if r:
                    what = '、'.join(sorted({'fire': '明火', 'smoke': '烟雾'}.get(l, l)
                                           for l in r['labels']))
                    heard = audio and audio.heard('smoke_alarm', within=60)
                    if r['level'] == 'confirmed' or heard:
                        s, c = evidence(frame, 'fire')
                        butler.notify('CRITICAL', '画面中持续出现%s%s，请立即确认！'
                                      % (what, '，同时听到报警器声' if heard else ''),
                                      event_type='fire', snapshot=s, clip=c)
                    else:
                        s, c = evidence(frame, 'fire_suspect')
                        butler.notify('GUARD', '疑似出现%s（置信度 %.2f），正在持续观察。'
                                      % (what, r['score']), event_type='fire_suspect',
                                      snapshot=s, clip=c)

            # ---- 人 ----
            person_info = []
            for t in person_tracks:
                c = t.center()
                pd = nearest_pose(t.bbox)
                kpts = pd.get('kpts') if pd else None
                evt = psm.update(t.tid, kpts, t.bbox, now)
                posture = psm.sm.get(t.tid, {}).get('state', 'unknown')
                person_info.append((t, kpts, posture))

                if evt == 'fall':
                    fall_ts[t.tid] = now
                    s, cl = evidence(frame, 'fall')
                    butler.notify('CRITICAL', '判断有人跌倒（#%d）！已抓拍并录像。' % t.tid,
                                  event_type='fall', snapshot=s, clip=cl)
                elif evt == 'lying_long':
                    s, cl = evidence(frame, 'lying')
                    butler.notify('CRITICAL', '有人倒地超过 %g 秒未起身，请查看。'
                                  % FALL_CFG['lying_min_secs'],
                                  event_type='lying_long', cooldown=600, snapshot=s, clip=cl)

                # 突发不适（有人在家时）
                if butler.mode in (HOME, NIGHT):
                    r = illness.update(t.tid, kpts, t.bbox, posture, c, zones, now, in_poly,
                                       fell_recently=now - fall_ts.get(t.tid, 0) < 600,
                                       heard_distress=bool(audio and audio.heard('distress', 30)),
                                       is_night=butler.mode == NIGHT)
                    if r:
                        level, et, text = r
                        s, cl = evidence(frame, et) if level == 'CRITICAL' else (snap(frame, et), None)
                        butler.notify(level, text, event_type=et, snapshot=s, clip=cl)

                # 卫生间久滞
                for z in zones.get('bathroom_zones', []):
                    if in_poly(c, z['poly']):
                        key = (t.tid, z['name'])
                        zone_enter.setdefault(key, now)
                        if now - zone_enter[key] > ZONE_DWELL_MIN * 60:
                            butler.notify('CRITICAL', '在%s已停留超过 %g 分钟。'
                                          % (z['name'], ZONE_DWELL_MIN), event_type='zone_dwell')
                    else:
                        zone_enter.pop((t.tid, z['name']), None)

                # 入侵：无人值守 / 宠物模式下出现确认的人形
                if butler.mode in (AWAY, PET) and t.hits >= 8:
                    s, cl = evidence(frame, 'intrusion')
                    sent = butler.notify('GUARD', '家中无人，但检测到移动的人形（#%d），'
                                         '已抓拍并录像。若是您本人，回复"是我"。' % t.tid,
                                         event_type='intrusion', snapshot=s, clip=cl)
                    if sent and args.follow and now - last_follow > 2:
                        dx = (c[0] - W / 2) / W
                        if abs(dx) > 0.15:
                            try:
                                from onvif_ptz import continuous_move, stop, get_profile_token
                                tok = get_profile_token()
                                continuous_move(tok, 0.4 if dx > 0 else -0.4, 0)
                                time.sleep(0.4)
                                stop(tok)
                            except Exception as e:
                                print('follow fail:', e)
                        last_follow = now

            illness.forget({t.tid for t in person_tracks})

            # 儿童危险
            if child and person_info:
                for level, et, text in child.update(person_info, zones, W, H, now, in_poly):
                    s, cl = evidence(frame, et) if level == 'CRITICAL' else (snap(frame, et), None)
                    butler.notify(level, text, event_type=et, snapshot=s, clip=cl)

            # 长时间无人出现
            absence = now - last_person_ts
            if absence > ABSENCE_MIN * 60 and now - last_absence > 3600:
                butler.notify('INFO', '家中已 %.1f 小时未检测到人。' % (absence / 3600),
                              event_type='absence')
                last_absence = now

            # 夜间离开卧室
            if butler.mode == NIGHT and person_tracks and zones.get('bedroom_zones'):
                in_bed = any(in_poly(t.center(), z['poly'])
                             for t in person_tracks for z in zones['bedroom_zones'])
                if not in_bed:
                    butler.notify('GUARD', '夜间检测到有人在卧室外活动。', event_type='night_wander')

            # ---- 宠物 ----
            for t in pet_tracks:
                for z in zones.get('cat_zones', []):
                    if in_poly(t.center(), z['poly']):
                        s = snap(frame, 'pet_zone')
                        butler.notify('PET', '%s上了%s！已抓拍。'
                                      % ('猫咪' if t.cls == CAT else '狗狗', z['name']),
                                      event_type='cat_zone', snapshot=s)
            pet_log.update(pet_tracks, zones, W, H, now, in_poly)
            person_log.update(person_tracks, zones, W, H, now, in_poly)
            if pet_unseen_h > 0 and butler.mode == PET:
                u = pet_log.unseen_for(now)
                if u and u > pet_unseen_h * 3600:
                    butler.notify('PET', '已 %.1f 小时没在画面里看到宠物，可能躲起来了，可以留意一下。'
                                  % (u / 3600), event_type='pet_unseen')

            # 每天存一张无人底图，用于热力图叠加
            if not tracks and now - last_base > 3600:
                cv2.imwrite(os.path.join(ev_dir, 'base_%s.jpg' % time.strftime('%Y%m%d')), frame)
                last_base = now

            if now - last_status > 30:
                print('[status] mode=%s persons=%d pets=%d audio=%s'
                      % (butler.mode, len(person_tracks), len(pet_tracks),
                         '%.0fdB' % audio.level_db if audio else '-'))
                last_status = now
            if now - last_save > 300:
                pet_log.save()
                person_log.save()
                last_save = now

            if not args.no_display:
                for t in tracks:
                    x1, y1, x2, y2 = map(int, t.bbox)
                    kid = child and t.cls == PERSON and child.is_child(t.tid)
                    cv2.rectangle(frame, (x1, y1), (x2, y2),
                                  (0, 200, 255) if kid else
                                  (0, 255, 0) if t.cls == PERSON else (255, 0, 255), 2)
                    cv2.putText(frame, '#%d %s' % (t.tid, 'child' if kid else CLASS_NAMES.get(t.cls, '?')),
                                (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.imshow('butler', frame)
                if cv2.waitKey(1) == 27:
                    break
    finally:
        pet_log.save()
        person_log.save()
        if clips:
            clips.flush()
        if audio:
            audio.stop()
        cap.release()


zone_enter = {}   # (tid, zone_name) -> ts

if __name__ == '__main__':
    main()
