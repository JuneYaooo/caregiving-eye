# -*- coding: utf-8 -*-
"""米执事感知核心 v2: 跟踪(track_id) + 姿态状态机 + 定时器族 + 模式联动 + 热力图
用法:
  python care_v2.py --source rtsp://127.0.0.1:8554/living --no-display
zones.json 结构:
  {"cat_zones":[{"name":"table","poly":[[x,y],...]}],
   "bathroom_zones":[...], "bedroom_zones":[...]}
"""
import argparse, json, os, time
import numpy as np
import cv2
from common_config import cfg
from simple_tracker import SimpleTracker
from butler import Butler, AWAY, NIGHT

PERSON, CAT, DOG = 0, 15, 16
CLASS_NAMES = {PERSON: 'person', CAT: 'cat', DOG: 'dog'}

FALL_CFG = {
    'stand_aspect_max': 0.9, 'lying_aspect_min': 1.1,
    'fall_transition_secs': 2.5, 'min_score': 0.4,
    'lying_min_secs': 30, 'lying_realert': 300,
}
CAT_CFG = {'min_score': 0.45, 'cooldown': 120}
ABSENCE_MIN = float(cfg('ABSENCE_MIN', 480))       # 无人出现告警(分钟)
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


def load_zones(path):
    if not os.path.exists(path):
        return {}
    z = json.load(open(path, encoding='utf-8'))
    return {k: z.get(k, []) for k in ('cat_zones', 'bathroom_zones', 'bedroom_zones')}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', default=cfg('RTSP_URL'))
    ap.add_argument('--det', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n.onnx'))
    ap.add_argument('--pose', default=os.path.join(cfg('MODELS_DIR', 'models'), 'yolov8n-pose.onnx'))
    ap.add_argument('--zones', default='zones.json')
    ap.add_argument('--heatmap-dir', default=cfg('EVENTS_DIR', 'events'))
    ap.add_argument('--no-display', action='store_true')
    ap.add_argument('--follow', action='store_true', help='AWAY 模式入侵时云台跟踪')
    ap.add_argument('--fps', type=float, default=3.0)
    ap.add_argument('--test-image', default='')
    args = ap.parse_args()

    from care_pipeline import YoloOnnx
    det_model = YoloOnnx(args.det)
    pose_model = YoloOnnx(args.pose) if os.path.exists(args.pose) else None
    butler = Butler()
    tracker = SimpleTracker()
    psm = PostureSM()
    zones = load_zones(args.zones)
    os.makedirs(args.heatmap_dir, exist_ok=True)
    grid_shape = (15, 26)
    grids = {'person': np.zeros(grid_shape, np.int32), 'pet': np.zeros(grid_shape, np.int32)}
    zone_enter = {}   # (tid, zone_name) -> ts
    last_absence_notify = 0
    last_follow = 0
    day_key = time.strftime('%Y%m%d')

    def flush_heatmap():
        day = time.strftime('%Y%m%d')
        np.save(os.path.join(args.heatmap_dir, 'heatmap_%s.npy' % day), grids['pet'])
        np.save(os.path.join(args.heatmap_dir, 'heatmap_person_%s.npy' % day), grids['person'])

    def grid_add(center, who):
        gx = int(center[0] / 2960 * grid_shape[1])
        gy = int(center[1] / 1666 * grid_shape[0])
        if 0 <= gx < grid_shape[1] and 0 <= gy < grid_shape[0]:
            grids[who][gy, gx] += 1

    if args.test_image:
        img = cv2.imread(args.test_image)
        for d in det_model(img):
            print('det', CLASS_NAMES.get(d['cls'], d['cls']), round(d['score'], 2))
        return

    cap = cv2.VideoCapture(args.source)
    if not cap.isOpened():
        print('ERROR: cannot open source', args.source)
        return
    n_warm = 0
    t0 = time.time()
    while n_warm < 40 and time.time() - t0 < 20:   # 等关键帧
        ok, _ = cap.read()
        if ok:
            n_warm += 1
    print('[care_v2] warm=%d frames, zones=%s, butler=%s mode=%s'
          % (n_warm, list(zones), butler.name, butler.mode))
    butler.notify('INFO', '%s上线，当前模式：%s。有事我叫你。' % (butler.name, butler.mode),
                  cooldown=0)
    last_ana, frame_id, last_hm = 0, 0, time.time()
    last_status = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.5)
            cap.release()
            cap = cv2.VideoCapture(args.source)
            continue
        frame_id += 1
        now = time.time()
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
        butler.update_presence(bool(person_tracks), only_pets=bool(pet_tracks) and not person_tracks)

        # 匹配姿态关键点到 person track (IoU 最大)
        def nearest_pose(bbox):
            best, bi = None, 0.2
            for p in pose_dets:
                i = SimpleTracker._iou(bbox, p['bbox'])
                if i > bi:
                    best, bi = p, i
            return best

        # ---- 人形: 姿态状态机 + zone 计时 + 入侵 ----
        for t in person_tracks:
            c = t.center()
            grid_add(c, 'person')
            pd = nearest_pose(t.bbox)
            evt = psm.update(t.tid, pd.get('kpts') if pd else None, t.bbox, now)
            if evt == 'fall':
                snap = os.path.join(args.heatmap_dir, 'snap_fall_%d.jpg' % frame_id)
                cv2.imwrite(snap, frame)
                butler.notify('CRITICAL', '判断%s在画面中跌倒！已抓拍。' % t.tid and
                              '判断有人跌倒（track #%d）！已抓拍。' % t.tid,
                              event_type='fall', snapshot=snap)
            elif evt == 'lying_long':
                snap = os.path.join(args.heatmap_dir, 'snap_lie_%d.jpg' % frame_id)
                cv2.imwrite(snap, frame)
                butler.notify('CRITICAL', '有人倒地已超过 %g 分钟未起身，请查看。'
                              % (FALL_CFG['lying_min_secs'] / 60),
                              event_type='lying_long', cooldown=600, snapshot=snap)
            # 卫生间久滞
            for z in zones.get('bathroom_zones', []):
                key = (t.tid, z['name'])
                if in_poly(c, z['poly']):
                    if key not in zone_enter:
                        zone_enter[key] = now
                    elif now - zone_enter[key] > ZONE_DWELL_MIN * 60:
                        butler.notify('CRITICAL', '在%s已滞留超过 %g 分钟。'
                                      % (z['name'], ZONE_DWELL_MIN),
                                      event_type='zone_dwell')
                else:
                    zone_enter.pop(key, None)
            # 入侵 (AWAY): 确认 track 才报
            if butler.mode == AWAY and t.hits >= 8:
                snap = os.path.join(args.heatmap_dir, 'snap_intr_%d.jpg' % frame_id)
                cv2.imwrite(snap, frame)
                sent = butler.notify('GUARD', '家中无人，但检测到移动的人形目标（track #%d），'
                                     '已抓拍存证。' % t.tid, event_type='intrusion', snapshot=snap)
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

        # 无人出现定时器
        if not person_tracks and person_tracks is not None:
            pass
        absence_secs = now - (tracker and max((t.last_seen for t in tracker.tracks.values()
                                               if t.cls == PERSON), default=0) or 0)
        if tracker.tracks and absence_secs > ABSENCE_MIN * 3600 and now - last_absence_notify > 3600:
            butler.notify('INFO', '家中已 %g 小时未检测到人，一切安静。'
                          % (absence_secs / 3600), event_type='absence')
            last_absence_notify = now

        # 夜间徘徊
        if butler.mode == NIGHT and person_tracks:
            in_bed = any(in_poly(t.center(), z['poly'])
                         for t in person_tracks for z in zones.get('bedroom_zones', []))
            if zones.get('bedroom_zones') and not in_bed:
                butler.notify('GUARD', '夜间检测到有人在卧室外活动。', event_type='night_wander')

        # ---- 宠物: 禁区 + 热力图 ----
        for t in pet_tracks:
            c = t.center()
            grid_add(c, 'pet')
            for z in zones.get('cat_zones', []):
                if in_poly(c, z['poly']):
                    butler.notify('PET', '%s上了%s！已存证。'
                                  % ('猫咪' if t.cls == CAT else '狗', z['name']),
                                  event_type='cat_zone', snapshot=None)

        # 心跳
        if now - last_status > 30:
            print('[status] mode=%s tracks=%s persons=%d pets=%d hm_p=%d hm_pet=%d'
                  % (butler.mode,
                     [(t.tid, CLASS_NAMES.get(t.cls, '?')) for t in tracks],
                     len(person_tracks), len(pet_tracks),
                     int(grids['person'].sum()), int(grids['pet'].sum())))
            last_status = now

        # 热力图每 5 分钟落盘
        if now - last_hm > 300 or time.strftime('%Y%m%d') != day_key:
            flush_heatmap()
            last_hm = now
            day_key = time.strftime('%Y%m%d')

        if not args.no_display:
            for t in tracks:
                x1, y1, x2, y2 = map(int, t.bbox)
                cv2.rectangle(frame, (x1, y1), (x2, y2),
                              (0, 255, 0) if t.cls == PERSON else (255, 0, 255), 2)
                cv2.putText(frame, '#%d %s' % (t.tid, CLASS_NAMES.get(t.cls, '?')),
                            (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.imshow('butler', frame)
            if cv2.waitKey(1) == 27:
                break

    flush_heatmap()
    cap.release()


if __name__ == '__main__':
    main()
