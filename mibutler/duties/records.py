# -*- coding: utf-8 -*-
"""记录与保养：活动统计、心跳、磁盘清理、底图、云台复位与画面对齐、跌倒样本、日报"""
import os, time
import cv2
from mibutler.butler import MODE_CN
from mibutler.brain import upkeep
from mibutler.zones import in_poly
from mibutler.config import cfg


def activity(app, c):
    """宠物 / 家人的热力图、活跃时长、区域停留、按小时分布"""
    now = time.time()
    if c.pets_fresh:     # 猫狗不是每轮都检测，只在本轮检测过时记录
        app.pet_log.update(c.pets, c.zones, c.W, c.H, now, in_poly)
    app.person_log.update(c.people, c.zones, c.W, c.H, now, in_poly)   # 只记合格的人，静物不污染作息基线


def save(app, c):
    app.pet_log.save()
    app.person_log.save()


def status(app, c):
    """心跳：每 30 秒写一次 events/status.json，守护进程和外部机器人靠它判断执事是否在岗"""
    f, a = app.frames, app.audio
    total = sum(app.level.time_in.values()) or 1
    upkeep.write_status(os.path.join(app.ev_dir, 'status.json'), {
        'time': time.strftime('%Y-%m-%d %H:%M:%S'),
        'uptime_h': round((time.time() - c.started) / 3600, 2),
        'mode': c.mode, 'mode_cn': MODE_CN.get(c.mode), 'level': c.level,
        'level_share': {k: round(v / total, 3) for k, v in app.level.time_in.items()},
        'zones_valid': c.zones_valid,
        'view': {'aligned': app.view.aligned, 'last_shift': app.view.last},
        'frames': {'connected': bool(f and f.connected), 'stream_fps': round(f.fps, 1) if f else 0,
                   'stale_sec': round(min(f.stale_for(), 9e5), 1) if f else None,
                   'reconnects': f.reconnects if f else 0},
        'audio': {'running': bool(a and a.is_alive()), 'level_db': round(a.level_db, 1) if a else None,
                  'inferences': a.inferences if a else 0},
        'tasks': app.sched.stats(),
        'open_incidents': app.incidents.snapshot(),
        'disk': app.disk,
    })


def retention(app, c):
    app.disk = upkeep.retention(app.ev_dir, keep_days=float(cfg('RETAIN_DAYS', 7)),
                                max_gb=float(cfg('MAX_EVENTS_GB', 5)),
                                cases_max_gb=float(cfg('CASES_MAX_GB', 2)))
    for name in ('alerts.jsonl', 'incidents.jsonl'):
        upkeep.rotate_log(os.path.join(app.ev_dir, name))


def base_image(app, c):
    """画面里没人时存一张当天底图，日报热力图叠加用"""
    frame, _, _ = app.frames.latest()
    if frame is not None:
        cv2.imwrite(os.path.join(app.ev_dir, 'base_%s.jpg' % time.strftime('%Y%m%d')), frame)


def ptz_return(app, c):
    app.ptz.maybe_return(target_present=bool(c.active))


def view_check(app, c):
    """画面对齐检查：云台复位漂移或被人手动转过时，暂停区域规则并提醒"""
    frame, _, _ = app.frames.latest()
    if frame is None:
        return
    r = app.view.check(frame)
    if r == 'shifted':
        dx, dy, _ = app.view.last
        app.signal('view_shift', 'GUARD', '画面和标定区域时相比偏移了约 %d%%（可能云台被转动过或复位不准），'
                   '卫生间、卧室、禁区等区域规则和热力图已暂停。请在米家 App 里把云台转回原位，'
                   '或重新标定区域。' % round(max(abs(dx), abs(dy)) * 100))
    elif r == 'restored':
        app.butler.notify('INFO', '画面已回到标定位置，区域规则恢复。', event_type='view_restored', cooldown=0)
    c.zones_valid = app.ptz.zones_valid and app.view.aligned


def samples_flush(app, c):
    """跌倒样本：事件后几秒把前后关键点写进 events/samples"""
    for path in app.samples.flush(time.time()):
        print('[samples] 已保存跌倒样本 %s' % os.path.basename(path))


def daily_report(app):
    from mibutler.report import daily_report as build
    text = build(events_dir=app.ev_dir)
    head = '\n'.join(text.splitlines()[:12])
    app.butler.notify('MORNING', head, event_type='daily_report', cooldown=0)
