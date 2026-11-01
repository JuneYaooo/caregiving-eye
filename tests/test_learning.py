# -*- coding: utf-8 -*-
"""越用越准：事后补标、漏报、家庭知识、每周复盘。python -m pytest tests -q"""
import json, os, sys, time, types
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, '.claude', 'skills', 'home-review', 'scripts'))
sys.modules.setdefault('onnxruntime', types.ModuleType('onnxruntime'))

from mibutler.brain import learning
from mibutler.brain.incidents import IncidentManager
from mibutler.butler import Butler
from mibutler.abilities.fall import MobilityFilter
from mibutler.abilities.presence import PersonGate
from mibutler.vision.tracker import SimpleTracker

DAY = time.strftime('%Y-%m-%d')


def closed(ev, hm, iid, types=('lying_long',), cells=('c1_2',), family='person_down', acked=None):
    with open(os.path.join(ev, 'incidents.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'id': iid, 'family': family, 'subject': 5, 'level': 'CRITICAL', 'types': list(types),
                            'opened': '%s %s:00' % (DAY, hm), 'duration_sec': 600, 'acked': acked,
                            'cells': list(cells)}, ensure_ascii=False) + '\n')


def manager(ev):
    b = Butler(events_path=os.path.join(ev, 'alerts.jsonl'))
    m = IncidentManager(b, ev)
    b.incidents = m
    return b, m


def test_retro_false_labels_feed_downgrade_and_samples(tmp_path):
    ev = str(tmp_path)
    os.makedirs(os.path.join(ev, 'samples'))
    json.dump({'incident': 'a1', 'label': None}, open(os.path.join(ev, 'samples', 'fall_x_a1.json'), 'w'))
    for i, hm in enumerate(('10:29', '11:40', '15:44')):
        closed(ev, hm, 'a%d' % (i + 1))
    b, m = manager(ev)
    for hm in ('10:29', '11:40', '15:44'):              # 主人事后补标三次（通过指令入口的同一路径）
        r = b.handle_command('没事 %s' % hm)
        assert '误报' in r, r
    assert json.load(open(os.path.join(ev, 'samples', 'fall_x_a1.json')))['label'] == 0
    # 同位置第 4 次：自动从急报降为值守，但照样提醒
    inc = m.signal('lying_long', 'CRITICAL', '有人倒地', subject=9, cell='c1_2')
    assert inc.level == 'GUARD' and '降一级' in inc.texts[-1]
    # 其他位置不受影响
    assert m.signal('lying_long', 'CRITICAL', '有人倒地', subject=10, cell='c4_1').level == 'CRITICAL'
    # 火情永远不降级
    assert m.signal('fire', 'CRITICAL', '火', cell='c1_2').level == 'CRITICAL'


def test_relabel_true_revokes_false(tmp_path):
    ev = str(tmp_path)
    closed(ev, '10:29', 'a1')
    b, m = manager(ev)
    b.handle_command('没事 10:29')
    assert len(m.feedback) == 1
    r = b.handle_command('属实 10:29')
    assert '属实' in r and not m.feedback
    assert not learning.read_jsonl(os.path.join(ev, 'feedback.jsonl'))


def test_open_incident_and_plain_replies_still_work(tmp_path):
    ev = str(tmp_path)
    b, m = manager(ev)
    m.signal('lying_long', 'CRITICAL', '有人倒地', subject=3, cell='c2_2')
    assert '没有待确认' not in b.handle_command('没事')          # 原来的“没事”照旧
    m.signal('fall', 'CRITICAL', '跌倒', subject=4, cell='c3_3')
    hm = time.strftime('%H:%M')
    assert '进行中' in b.handle_command('属实 %s' % hm)
    assert b.handle_command('没事了') is not None and '找到' not in b.handle_command('没事了')


def test_missed_and_offline_write(tmp_path):
    ev = str(tmp_path)
    r = learning.apply_label_command(ev, '漏报 15:00 有人进来没报')
    assert '记下了' in r
    rows = learning.read_jsonl(os.path.join(ev, 'labels.jsonl'))
    assert rows[0]['kind'] == 'missed' and rows[0]['when'] == '15:00'
    assert '没找到' in learning.apply_label_command(ev, '没事 03:00')


def test_household_static_object_blocks_statue_immediately(tmp_path):
    ev = str(tmp_path)
    import household as hh
    hh.main(['--events-dir', ev, 'add-object', '--name', '石膏像', '--roi', '0.55,0.3,0.7,0.8'])
    H = learning.Household(os.path.join(ev, 'household.json'))
    gate, tr = PersonGate(MobilityFilter()), SimpleTracker()
    W, Hh = 1920, 1080
    t = 0.0
    people = None
    for i in range(120):                 # 框大幅抖动，骗得过“动过”，但在静物区内
        cx = 1200 + (90 if i % 5 == 0 else 0)
        box = {'bbox': [cx - 100, 340, cx + 100, 740], 'score': .8, 'cls': 0}
        tracks = tr.update([box], t)
        active, people = gate.update(tracks, {x.tid: True for x in tracks}, W, Hh, t,
                                     static=lambda pt: H.static_at(pt, W, Hh))
        t += 1
    assert not people and not active
    # 真人走进静物区：照样算人
    tr2, gate2 = SimpleTracker(), PersonGate(MobilityFilter())
    for i in range(30):
        x = 200 + i * 40
        tracks = tr2.update([{'bbox': [x - 100, 340, x + 100, 740], 'score': .8, 'cls': 0}], t)
        active, people = gate2.update(tracks, {k.tid: True for k in tracks}, W, Hh, t,
                                      static=lambda pt: H.static_at(pt, W, Hh))
        t += 1
    assert len(people) == 1
    d = json.load(open(os.path.join(ev, 'household.json'), encoding='utf-8'))
    assert d['changelog'][-1]['what'].startswith('静物')


def test_weekly_review_suggests_static_and_unlabeled(tmp_path):
    ev = str(tmp_path)
    for i, hm in enumerate(('10:29', '11:40', '12:10')):
        closed(ev, hm, 'f%d' % i, cells=('c1_2',), acked='false')
    for i, hm in enumerate(('16:00', '16:20', '16:40')):
        closed(ev, hm, 'u%d' % i, types=('intrusion',), cells=('c5_3',), family='security')
    learning.missed(ev, '15:00', '有人进来没报')
    import weekly_review as wr
    r = wr.review(ev, 7)
    kinds = [s['kind'] for s in r['suggestions']]
    assert 'static_object' in kinds and 'unlabeled' in kinds and 'missed' in kinds
    so = [s for s in r['suggestions'] if s['kind'] == 'static_object'][0]
    assert '0.167,0.5,0.333,0.75' in so['command']
    assert '没事' in wr.to_text(r)


def test_day_digest_uses_retro_labels(tmp_path):
    ev = str(tmp_path)
    closed(ev, '10:29', 'a1')
    with open(os.path.join(ev, 'alerts.jsonl'), 'w', encoding='utf-8') as f:
        f.write(json.dumps({'ts': '%s 10:29:30' % DAY, 'level': 'CRITICAL', 'type': 'lying_long',
                            'text': '〔急报〕 有人倒地'}, ensure_ascii=False) + '\n')
    learning.label(ev, '10:29', 'false')
    learning.missed(ev, '15:00', '有人进来没报')
    import day_digest as dd
    d = dd.digest(ev, DAY.replace('-', ''))
    assert d['timeline'][0]['verdict'] == '主人确认误报'
    assert d['counts']['missed'] == 1
