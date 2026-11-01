# -*- coding: utf-8 -*-
"""宠物：禁区提醒、长时间没露面（活动统计在 records.activity）"""
import time
from mibutler.vision.yolo import CAT
from mibutler.zones import in_poly
from mibutler.config import cfg


def pet_zones(app, c):
    for t in c.pets:
        for z in c.zones.get('cat_zones', []):
            if in_poly(t.center(), z['poly']):
                app.signal('cat_zone', 'PET', '%s上了%s！已抓拍。'
                           % ('猫咪' if t.cls == CAT else '狗狗', z['name']), subject=z['name'])


def pet_unseen(app, c):
    hrs = float(cfg('PET_UNSEEN_HOURS', 0))
    u = app.pet_log.unseen_for(time.time())
    if hrs > 0 and u and u > hrs * 3600:
        app.signal('pet_unseen', 'PET', '已 %.1f 小时没看到宠物，可能躲起来了，可以留意一下。' % (u / 3600))
