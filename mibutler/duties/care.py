# -*- coding: utf-8 -*-
"""照护：突发不适、儿童危险、卫生间久滞、夜间离床、作息偏离、长时间无人"""
import time
from mibutler.butler import NIGHT
from mibutler.zones import in_poly
from mibutler.config import cfg

ABSENCE_MIN = float(cfg('ABSENCE_MIN', 480))       # 长时间无人出现提醒（分钟）
ZONE_DWELL_MIN = float(cfg('ZONE_DWELL_MIN', 20))  # 卫生间久滞（分钟）


def illness(app, c):
    """多信号合并判断突发不适；呼喊呻吟声是佐证之一"""
    a = app.audio
    distress = bool(a and a.heard('distress', 30))
    for t in c.persons:
        if not app.mobility.moved(t.tid):
            continue  # 家具化：静止摆件不参与突发不适研判
        r = app.illness.update(t.tid, c.kpts.get(t.tid), t.bbox, c.posture.get(t.tid, 'unknown'),
                               t.center(), c.zones if c.zones_valid else {}, time.time(), in_poly,
                               fell_recently=time.time() - c.fall_ts.get(t.tid, 0) < 600,
                               heard_distress=distress, is_night=c.mode == NIGHT)
        if r:
            lvl, et, text = r
            app.signal(et, lvl, text, subject=t.tid, pt=t.center())
    app.illness.forget({t.tid for t in c.persons})


def child(app, c):
    info = [(t, c.kpts.get(t.tid), c.posture.get(t.tid, 'unknown')) for t in c.people]
    for lvl, et, text in app.child.update(info, c.zones, c.W, c.H, time.time(), in_poly):
        app.signal(et, lvl, text, pt=None)
    app.child.forget(set(app.tracker.tracks))


def person_zones(app, c):
    """卫生间久滞、夜间离开卧室（只看合格的人：雕像、挂衣不会“久滞”）"""
    now = time.time()
    for t in c.people:
        for z in c.zones.get('bathroom_zones', []):
            key = (t.tid, z['name'])
            if in_poly(t.center(), z['poly']):
                c.zone_enter.setdefault(key, now)
                if now - c.zone_enter[key] > ZONE_DWELL_MIN * 60:
                    app.signal('zone_dwell', 'CRITICAL', '在%s已停留超过 %g 分钟。'
                               % (z['name'], ZONE_DWELL_MIN), subject=t.tid, pt=t.center())
            else:
                c.zone_enter.pop(key, None)
    for key in list(c.zone_enter):
        if key[0] not in app.tracker.tracks:      # 目标已被跟踪器丢弃
            c.zone_enter.pop(key)
    if c.mode == NIGHT and c.people and c.zones.get('bedroom_zones'):
        if not any(in_poly(t.center(), z['poly']) for t in c.people for z in c.zones['bedroom_zones']):
            app.signal('night_wander', 'GUARD', '夜间检测到有人在卧室外活动。', pt=c.people[0].center())


def routine(app, c):
    """作息偏离：起床晚、活动少、夜里多动"""
    with app.person_log._lock:
        stats = dict(app.person_log.stats)
    for lvl, et, text in app.routine.check(stats, time.time(), c.person_seen_ts):
        app.signal(et, lvl, text)


def absence(app, c):
    gone = time.time() - c.last_person_ts
    if gone > ABSENCE_MIN * 60:
        app.signal('absence', 'INFO', '家中已 %.1f 小时未检测到人。' % (gone / 3600))
