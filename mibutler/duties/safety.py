# -*- coding: utf-8 -*-
"""安全：火焰烟雾、入侵（无人时）"""
import time
from mibutler.config import cfg

FIRE_LABEL_CN = {'fire': '明火', 'smoke': '烟雾'}


def fire(app, c):
    """火焰烟雾视觉检测；同时听到报警器声直接急报"""
    frame, ts, _ = app.frames.latest()
    if frame is None:
        return
    r = app.fire.update(frame, ts)
    if not r:
        return
    what = '、'.join(sorted(FIRE_LABEL_CN.get(l, l) for l in r['labels']))
    a = app.audio
    heard = bool(a and a.heard('smoke_alarm', within=60))
    if r['level'] == 'confirmed' or heard:
        app.signal('fire', 'CRITICAL', '画面中持续出现%s%s，请立即确认！'
                   % (what, '，同时听到报警器声' if heard else ''))
    else:
        app.signal('fire_suspect', 'GUARD', '疑似出现%s（置信度 %.2f），正在加密观察。' % (what, r['score']))


def intrusion(app, c):
    """家里没人时出现移动的人形：抓拍、录像，可选云台跟踪"""
    for t in c.persons:
        if t.hits < 8:
            continue
        if not app.mobility.moved(t.tid):
            continue  # 家具化：静止摆件不算入侵者
        app.signal('intrusion', 'GUARD', '家中无人，但检测到移动的人形（#%d），已抓拍并录像。'
                   '若是您本人，回复"是我"。' % t.tid, pt=t.center())
        dx = (t.center()[0] - c.W / 2) / c.W
        if app.ptz.can_nudge() and abs(dx) >= 0.15:
            app.ptz.request()     # 先占位，转完之前不再排新的转动，避免按旧画面连转过头
            app.sched.once('ptz_follow', lambda d=dx: app.ptz.nudge(d), lane='io')
        break
