# -*- coding: utf-8 -*-
"""home-review skill 的两个脚本：python -m pytest tests -q"""
import json, os, sys
import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), '.claude', 'skills', 'home-review', 'scripts'))
import scene_diff as sd
import day_digest as dd

TABLE = (0.55, 0.55, 0.85, 0.85)   # 餐桌 ROI


def room(bright=1.0, bag=False, person_x=None, seed=0):
    r = np.random.RandomState(seed)
    img = np.full((540, 960, 3), 170, np.uint8)
    for i in range(0, 960, 60):                      # 墙上的竖纹、地板格
        cv2.line(img, (i, 0), (i, 300), (120, 120, 120), 2)
    for j in range(300, 540, 40):
        cv2.line(img, (0, j), (960, j), (100, 100, 100), 2)
    cv2.rectangle(img, (530, 300), (820, 460), (90, 60, 40), -1)      # 桌子
    cv2.rectangle(img, (100, 120), (220, 420), (230, 230, 230), -1)   # 石膏像
    cv2.circle(img, (160, 110), 40, (230, 230, 230), -1)
    if bag:
        cv2.rectangle(img, (650, 330), (740, 420), (250, 250, 250), -1)
        for k in range(650, 740, 12):
            cv2.line(img, (k, 330), (k + 6, 420), (200, 200, 200), 2)
    if person_x is not None:
        cv2.rectangle(img, (person_x, 150), (person_x + 90, 500), (40, 40, 160), -1)
    img = np.clip(img.astype(np.float32) * bright + r.normal(0, 3, img.shape), 0, 255).astype(np.uint8)
    return img


def test_compare_detects_bag_not_lighting():
    a, b = room(), room(bag=True)
    r = sd.compare(a, b, TABLE)
    assert r['changed'] and r['boxes']
    x1, y1, x2, y2 = r['boxes'][0]
    assert 0.6 < (x1 + x2) / 2 < 0.8, r['boxes']
    light = sd.compare(room(), room(bright=0.6, seed=1), TABLE)    # 傍晚变暗
    assert not light['changed'], light


def test_bisect_on_video_finds_moment_despite_passerby(tmp_path):
    """10 分钟录像：第 200 秒有人路过挡住桌子，第 400 秒袋子放下 → 二分应定位到 400 秒附近"""
    path = str(tmp_path / 'rec.mp4')
    fps = 1
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (960, 540))
    for t in range(600):
        px = 640 if 198 <= t <= 203 else (640 if 396 <= t <= 401 else None)
        w.write(room(bright=1.0 - 0.3 * t / 600, bag=t >= 400, person_x=px, seed=t))
    w.release()
    src = sd.Source(video=path)
    scan = sd.scan(src, 0, 599, 60, TABLE)
    real = [x for x in scan if not x['lighting']]
    assert any(x['from'] <= 400 <= x['to'] for x in real), scan
    r = sd.bisect(src, 0, 599, TABLE)
    # 定位到的通常是“人走到桌前”那一刻（393~396 秒挡住桌子），即放东西的人，正是想看的那帧
    assert r['found'] and 390 <= r['after'] <= 406, r
    assert r['steps'] <= 12


def test_bisect_reports_no_change_for_lighting_only(tmp_path):
    path = str(tmp_path / 'rec.mp4')
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'mp4v'), 1, (960, 540))
    for t in range(300):
        w.write(room(bright=1.0 - 0.4 * t / 300, seed=t))
    w.release()
    r = sd.bisect(sd.Source(video=path), 0, 299, TABLE)
    assert not r['found']


def _write(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def test_day_digest_merges_and_labels(tmp_path):
    ev = tmp_path
    day = '2026-11-01'
    alerts = [
        {'ts': day + ' 00:03:00', 'level': 'INFO', 'type': 'mode_change', 'text': '〔执事〕 切换到夜间加强（按日程）。'},
        {'ts': day + ' 10:12:00', 'level': 'INFO', 'type': 'arrival', 'text': '〔执事〕 有人回来了，当前在家 1 人。',
         'snapshot': 'events/snap_arrival_20261101_101200.jpg'},
    ] + [
        {'ts': day + ' 10:%02d:00' % m, 'level': 'CRITICAL', 'type': 'lying_long',
         'text': '〔急报〕 有人倒地超过 30 秒未起身（#5），请查看。', 'snapshot': 'events/snap_lying_long_20261101_10%02d00.jpg' % m}
        for m in (29, 33, 39)
    ] + [
        {'ts': day + ' 10:41:00', 'level': 'INFO', 'type': 'departure', 'text': '〔执事〕 人都出门了，约 9 分钟后进入无人值守。'},
        {'ts': day + ' 10:45:00', 'level': 'INFO', 'type': 'incident_close', 'text': '〔执事〕 收尾'},
        {'ts': day + ' 15:44:00', 'level': 'CRITICAL', 'type': 'lying_long', 'text': '〔急报〕 有人倒地超过 30 秒未起身（#152），请查看。'},
        {'ts': '2026-10-31 23:00:00', 'level': 'CRITICAL', 'type': 'fall', 'text': '昨天的'},
    ]
    _write(ev / 'alerts.jsonl', alerts)
    _write(ev / 'incidents.jsonl', [
        {'id': 'a', 'family': 'person_down', 'types': ['lying_long'], 'opened': day + ' 10:29:00',
         'duration_sec': 700, 'acked': 'false'},
    ])
    d = dd.digest(str(ev), '20261101')
    ev_rows = [r for r in d['timeline'] if r['kind'] == 'event']
    assert len(ev_rows) == 2, ev_rows                     # 10:29/33/39 合并成一行；15:44 单独一行
    assert ev_rows[0]['count'] == 3 and ev_rows[0]['verdict'] == '主人确认误报'
    assert ev_rows[1]['verdict'] == '未确认'
    assert d['counts']['presence'] == 2
    assert not any(r['type'] == 'incident_close' for r in d['timeline'])
    assert '昨天的' not in json.dumps(d, ensure_ascii=False)
    text = dd.to_text(d)
    assert '共 3 条' in text and '主人确认误报' in text
    w = dd.digest(str(ev), '20261101', '15:00', '18:00')
    assert len(w['timeline']) == 1
