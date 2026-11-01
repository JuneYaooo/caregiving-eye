# -*- coding: utf-8 -*-
"""区域标定：读取 zones.json（格式见 zones.example.json）

  cat_zones / pet_zones   宠物禁区、宠物常去处          bathroom_zones  卫生间（久滞、倒卧）
  bedroom_zones           卧室（夜间离床）              rest_zones      床、沙发（躺着不算倒地）
  danger_zones            灶台、窗台、大门（儿童危险）
多边形坐标是标定那一刻画面的像素坐标；云台转动后画面对不上，区域规则会自动暂停（见 brain/ptz.py）。
"""
import json, os
import numpy as np
import cv2

ZONE_KEYS = ('cat_zones', 'pet_zones', 'bathroom_zones', 'bedroom_zones',
             'rest_zones', 'danger_zones')


def load_zones(path):
    if not path or not os.path.exists(path):
        return {}
    z = json.load(open(path, encoding='utf-8'))
    return {k: z.get(k, []) for k in ZONE_KEYS}


def in_poly(pt, poly):
    return cv2.pointPolygonTest(np.array(poly, np.float32), pt, False) >= 0


def cell_of(pt, W, H):
    """把画面分成 6×4 格，误报学习按格子记"""
    return 'c%d_%d' % (min(int(pt[0] / W * 6), 5), min(int(pt[1] / H * 4), 3))
