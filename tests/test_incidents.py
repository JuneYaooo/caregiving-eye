# -*- coding: utf-8 -*-
"""事件研判：倒地急报的当事人自己站起来后停止催办"""
import os, sys, types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if 'onnxruntime' not in sys.modules:
    sys.modules['onnxruntime'] = types.ModuleType('onnxruntime')

from mibutler.abilities.fall import PostureSM
from mibutler.brain.incidents import IncidentManager


class FakeButler:
    def __init__(self):
        self.sent = []

    def notify(self, level, text, **kw):
        self.sent.append((level, text))
        return True


def stand_kpts():
    k = [[0, 0, 0.0]] * 17
    k[5], k[6], k[11], k[12] = [100, 100, .9], [140, 100, .9], [100, 200, .9], [140, 200, .9]
    return k


def test_recovered_stops_reminders(tmp_path):
    b = FakeButler()
    m = IncidentManager(b, str(tmp_path), remind_sec=180)
    m.signal('lying_long', 'CRITICAL', '有人倒地', subject=180, now=1000)
    m.tick(now=1200)
    assert sum('第 1 次提醒' in t for _, t in b.sent) == 1
    assert m.recovered(180, now=1250) == 1
    m.tick(now=1500)
    m.tick(now=1800)
    assert not any('第 2 次提醒' in t for _, t in b.sent)
    assert m.highest_open() < 0
    # 自己站起来后仍可补标误报
    assert m.ack('false') == 1
    # 又倒下：按新事件重新提醒
    m2 = IncidentManager(b, str(tmp_path))
    m2.signal('fall', 'CRITICAL', '跌倒', subject=7, now=1000)
    m2.recovered(7, now=1010)
    n = len(b.sent)
    m2.signal('lying_long', 'CRITICAL', '又倒地', subject=7, now=1060)
    assert len(b.sent) == n + 1 and m2.highest_open() == 3


def test_recovered_ignores_other_people_and_info(tmp_path):
    b = FakeButler()
    m = IncidentManager(b, str(tmp_path))
    m.signal('lying_long', 'CRITICAL', '有人倒地', subject=1, now=1000)
    assert m.recovered(2, now=1010) == 0
    assert m.highest_open() == 3


def test_standing_for_needs_sustained_stand():
    sm = PostureSM()
    k = stand_kpts()
    for i in range(10):
        sm.update(1, k, (100, 50, 140, 300), now=1000 + i)
    assert sm.standing_for(1, 1009) >= 5
    assert sm.standing_for(2, 1009) == 0


def test_labeled_evidence_survives_retention(tmp_path):
    import json, time
    from mibutler.brain.upkeep import retention
    ev = tmp_path
    old = time.time() - 30 * 86400
    for name in ('snap_fall_a.jpg', 'clip_fall_a.mp4', 'snap_fall_b.jpg', 'snap_x.jpg'):
        p = ev / name
        p.write_bytes(b'x' * 100)
        os.utime(p, (old, old))
    with open(ev / 'incidents.jsonl', 'w', encoding='utf-8') as f:
        f.write(json.dumps({'id': 'A', 'acked': 'false', 'snapshot': str(ev / 'snap_fall_a.jpg'),
                            'clip': str(ev / 'clip_fall_a.mp4')}) + '\n')
        f.write(json.dumps({'id': 'B', 'acked': None, 'snapshot': str(ev / 'snap_fall_b.jpg')}) + '\n')
    with open(ev / 'labels.jsonl', 'w', encoding='utf-8') as f:     # 事后补标的也保留
        f.write(json.dumps({'kind': 'true', 'incident': 'B'}) + '\n')
    retention(str(ev), keep_days=7)
    left = sorted(p.name for p in ev.iterdir() if p.suffix in ('.jpg', '.mp4'))
    assert left == ['clip_fall_a.mp4', 'snap_fall_a.jpg', 'snap_fall_b.jpg']
