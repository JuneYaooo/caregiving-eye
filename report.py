# -*- coding: utf-8 -*-
"""执事日报: 热力图渲染 + 事件摘要
用法: python report.py [YYYYMMDD]   (默认今天)
"""
import json, os, sys, time
import numpy as np
import cv2
from common_config import cfg
from butler import Butler, MODE_CN

EVENTS_DIR = cfg('EVENTS_DIR', 'events')


def load_heatmap(day, who='pet'):
    p = os.path.join(EVENTS_DIR, 'heatmap_%s_%s.npy' % (who, day))
    return np.load(p) if os.path.exists(p) else None


def render_heatmap(grid, base_image=None, out='heatmap.png'):
    if grid is None or grid.max() == 0:
        return None
    g = grid.astype(np.float32) / grid.max()
    g = cv2.GaussianBlur(g, (5, 5), 2)
    heat = cv2.applyColorMap((g * 255).astype(np.uint8), cv2.COLORMAP_JET)
    heat[grid == 0] = 0
    if base_image and os.path.exists(base_image):
        base = cv2.imread(base_image)
        base = cv2.resize(base, (heat.shape[1], heat.shape[0]))
        out_img = cv2.addWeighted(base, 0.55, heat, 0.45, 0)
    else:
        out_img = 255 - heat  # 白底热力
    cv2.imwrite(out, out_img)
    return out


def daily_report(day=None):
    day = day or time.strftime('%Y%m%d')
    b = Butler()
    # 事件摘要
    evs = []
    path = os.path.join(EVENTS_DIR, 'alerts.jsonl')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                e = json.loads(line)
                if e['ts'].startswith(day[:4] + '-' + day[4:6] + '-' + day[6:8]):
                    evs.append(e)
            except Exception:
                continue
    by_type = {}
    for e in evs:
        by_type[e.get('type', e.get('level'))] = by_type.get(e.get('type', e.get('level')), 0) + 1
    lines = ['〔晨报〕 %s' % day, '模式：%s。' % MODE_CN[b.mode]]
    if evs:
        lines.append('通报 %d 条：' % len(evs))
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
            lines.append('  - %s ×%d' % (k, v))
    else:
        lines.append('全天无异常通报，一切如常。')
    # 热力图
    out_path = os.path.join(EVENTS_DIR, 'report_heatmap_%s.png' % day)
    img = render_heatmap(load_heatmap(day, 'pet'), 'ptz_before.jpg', out_path)
    pet_grid = load_heatmap(day, 'pet')
    if pet_grid is not None and pet_grid.max() > 0:
        total = int(pet_grid.sum())
        lines.append('宠物活动采样 %d 点，热力图：%s' % (total, img or '活动过少'))
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(EVENTS_DIR, 'report_%s.md' % day), 'w', encoding='utf-8') as f:
        f.write(text)
    return text


if __name__ == '__main__':
    daily_report(sys.argv[1] if len(sys.argv) > 1 else None)
