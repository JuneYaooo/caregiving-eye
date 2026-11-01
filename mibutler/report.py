# -*- coding: utf-8 -*-
"""执事日报：事件摘要 + 宠物 / 人活动总结 + 热力图
用法: python main.py report [YYYYMMDD]   (默认今天)
输出: events/report_YYYYMMDD.md 与 events/report_heatmap_<pet|person>_YYYYMMDD.png
"""
import json, os, sys, time
import numpy as np
import cv2
from mibutler.config import cfg
from mibutler.abilities.activity import summarize
from mibutler.abilities.routine import RoutineMonitor

EVENTS_DIR = cfg('EVENTS_DIR', 'events')

TYPE_CN = {
    'intrusion': '入侵', 'fire': '火情', 'fire_suspect': '疑似烟火', 'fire_alarm_sound': '报警器声',
    'glass_break': '玻璃碎裂声', 'absence': '长时间无人', 'fall': '跌倒', 'lying_long': '倒地久卧',
    'illness': '疑似突发不适', 'inactive': '长时间无动作', 'zone_dwell': '区域久滞',
    'night_wander': '夜间离床', 'child_danger': '儿童危险', 'child_danger_watched': '儿童靠近危险区',
    'child_alone': '儿童独处', 'baby_cry': '婴儿哭声', 'cat_zone': '宠物进禁区',
    'pet_distress': '宠物嘶叫', 'pet_unseen': '宠物久未出现', 'mode_change': '模式切换',
    'fall_suspect': '疑似跌倒', 'routine_late': '起床偏晚', 'routine_low': '活动偏少',
    'routine_night': '夜间活动偏多', 'view_shift': '镜头偏离标定位置', 'stream_down': '画面中断',
    'supervisor': '执事重启', 'arrival': '有人回来', 'departure': '人都出门',
}
PRESENCE_TYPES = ('arrival', 'departure')


def _load(day, kind, events_dir):
    jp = os.path.join(events_dir, 'activity_%s_%s.json' % (kind, day))
    hp = os.path.join(events_dir, 'heatmap_%s_%s.npy' % (kind, day))
    stats = json.load(open(jp, encoding='utf-8')) if os.path.exists(jp) else None
    grid = np.load(hp) if os.path.exists(hp) else None
    return stats, grid


def render_heatmap(grid, base_image=None, out='heatmap.png'):
    if grid is None or grid.max() == 0:
        return None
    g = grid.astype(np.float32) / grid.max()
    base = cv2.imread(base_image) if base_image and os.path.exists(base_image) else None
    size = (base.shape[1], base.shape[0]) if base is not None else (1040, 600)
    g = cv2.resize(g, size, interpolation=cv2.INTER_CUBIC)
    g = cv2.GaussianBlur(g, (0, 0), size[0] / 60)
    g = np.clip(g / max(g.max(), 1e-6), 0, 1)
    heat = cv2.applyColorMap((g * 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    if base is not None:
        alpha = (g[..., None] * 0.65).astype(np.float32)
        out_img = (base * (1 - alpha) + heat * alpha).astype(np.uint8)
    else:
        out_img = heat
    cv2.imwrite(out, out_img)
    return out


def _hour_bar(hourly):
    """24 小时活跃度的文字条形图"""
    blocks = ' ▁▂▃▄▅▆▇█'
    m = max(hourly) or 1
    return ''.join(blocks[int(v / m * 8)] for v in hourly)


def daily_report(day=None, events_dir=None):
    """events_dir 默认取 .env 的 EVENTS_DIR；执事用 --events-dir 改过目录时会传进来"""
    ev_dir = events_dir or EVENTS_DIR
    day = day or time.strftime('%Y%m%d')
    date = '%s-%s-%s' % (day[:4], day[4:6], day[6:8])
    evs = []
    path = os.path.join(ev_dir, 'alerts.jsonl')
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                e = json.loads(line)
                if e['ts'].startswith(date):
                    evs.append(e)
            except Exception:
                continue

    lines = ['〔日报〕%s' % date, '']
    serious = [e for e in evs if e.get('level') in ('CRITICAL', 'GUARD')]
    by_type = {}
    for e in evs:
        k = e.get('type', e.get('level'))
        if k != 'mode_change' and k not in PRESENCE_TYPES:
            by_type[k] = by_type.get(k, 0) + 1
    if by_type:
        lines.append('## 事件')
        for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]):
            lines.append('- %s ×%d' % (TYPE_CN.get(k, k), v))
        if serious:
            lines.append('')
            lines.append('重要事件：')
            for e in serious[-10:]:
                extra = '（录像 %s）' % e['clip'] if e.get('clip') else ''
                lines.append('- %s %s%s' % (e['ts'][11:16], e['text'], extra))
    else:
        lines.append('全天没有需要提醒的事件。')

    pending = [e for e in serious if e.get('type') not in ('stream_down', 'view_shift', 'supervisor')]
    if pending:
        lines += ['', '帮执事变准：上面的提醒如果是误报，回复“没事 时间”（如“没事 %s”），真事件回复“属实 时间”；'
                  '有事没提醒，回复“漏报 时间 描述”。' % pending[-1]['ts'][11:16]]

    moves = [e for e in evs if e.get('type') in PRESENCE_TYPES]
    if moves:
        lines += ['', '## 进出']
        for e in moves:
            text = e['text'].split(' ', 1)[-1]
            lines.append('- %s %s%s' % (e['ts'][11:16], text,
                                        '（快照 %s）' % e['snapshot'] if e.get('snapshot') else ''))

    base = os.path.join(ev_dir, 'base_%s.jpg' % day)
    for kind, label in (('pet', '宠物'), ('person', '家人')):
        stats, grid = _load(day, kind, ev_dir)
        lines += ['', '## %s活动' % label]
        lines += summarize(stats, label)
        if stats and max(stats.get('hourly_active') or [0]) > 0:
            lines.append('  24 小时活跃度：`%s`' % _hour_bar(stats['hourly_active']))
        if kind == 'person':
            r = RoutineMonitor.from_env(ev_dir, cfg)
            if r:
                lines += ['  ' + x for x in r.summary(stats, day)]
        img = render_heatmap(grid, base, os.path.join(ev_dir, 'report_heatmap_%s_%s.png'
                                                      % (kind, day)))
        if img:
            lines.append('  热力图：%s' % img)

    text = '\n'.join(lines)
    print(text)
    os.makedirs(ev_dir, exist_ok=True)
    with open(os.path.join(ev_dir, 'report_%s.md' % day), 'w', encoding='utf-8') as f:
        f.write(text)
    return text
