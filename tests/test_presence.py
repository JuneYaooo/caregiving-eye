# -*- coding: utf-8 -*-
"""“谁算人”与进出通报：python -m pytest tests -q"""
import os, sys, types
from types import SimpleNamespace
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if 'onnxruntime' not in sys.modules:          # 测试不跑真模型
    sys.modules['onnxruntime'] = types.ModuleType('onnxruntime')

from mibutler.abilities.fall import MobilityFilter
from mibutler.abilities.presence import PersonGate
from mibutler.vision.tracker import SimpleTracker
from mibutler.butler import Butler, AWAY, HOME

W, H = 1920, 1080
KPTS = [[0, 0, 0.9]] * 17          # “可信”的关键点（雕像也能骗过这一关）


def box(cx, cy, w=200, h=400):
    return {'bbox': [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 'score': 0.8, 'cls': 0}


def run(gate, tracker, frames, t0=0.0, dt=1.0):
    """frames: [[(cx, cy), ...], ...]；返回最后一轮 (active, people, tracks) 和结束时间"""
    t = t0
    out = None
    for pts in frames:
        tracks = tracker.update([box(*p) for p in pts], t)
        active, people = gate.update(tracks, {tr.tid: True for tr in tracks}, W, H, t)
        out = (active, people, tracks)
        t += dt
    return out, t


def jitter(cx, cy, n, amp=30, seed=0):
    r = np.random.RandomState(seed)
    return [[(cx + r.uniform(-amp, amp), cy + r.uniform(-amp, amp))] for _ in range(n)]


def new_gate(tmp_path=None):
    return PersonGate(MobilityFilter(), path=str(tmp_path / 'furn.json') if tmp_path else None)


def test_statue_never_counts():
    """雕像框中心小幅抖动（< 4% 画面宽）：不动、不算人、不触发警觉"""
    gate, tr = new_gate(), SimpleTracker()
    (active, people, tracks), _ = run(gate, tr, jitter(1200, 600, 600))
    assert tracks and not active and not people


def test_statue_with_big_jitter_blocked_by_furniture_memory(tmp_path):
    """下午光线变化让框大幅跳动、骗过“动过”判断：运行满 30 分钟后被家具格拉黑，且重启后立即生效"""
    gate, tr = new_gate(tmp_path), SimpleTracker()
    frames = [[(1200 + (90 if i % 7 == 0 else 0), 600)] for i in range(2400)]
    (active, people, _), t = run(gate, tr, frames)
    assert not people, '家具格应拉黑雕像'
    gate.save()
    gate2, tr2 = new_gate(tmp_path), SimpleTracker()
    (_, people2, _), _ = run(gate2, tr2, frames[:60], t0=t)
    assert not people2, '重启后家具记忆应立即生效'


def test_real_person_walks_into_furniture_cell(tmp_path):
    """真人走进来坐在雕像旁边：走过路的人不受家具格影响"""
    gate, tr = new_gate(tmp_path), SimpleTracker()
    (_, _, _), t = run(gate, tr, jitter(1200, 600, 2400))             # 先让雕像所在格成为家具格
    assert gate.furniture((1200, 600), W, H)
    walk = [[(1200, 600), (300 + i * 40, 600)] for i in range(23)]     # 从左边走到雕像旁
    sit = [[(1200, 600), (1180, 650)]] * 60
    (_, people, _), _ = run(gate, SimpleTracker(), walk + sit, t0=t)
    assert len(people) == 1, '在家人数应为 1（只有真人）'


def test_seated_person_id_switch_keeps_presence():
    """坐着不动的人跟踪丢失后换了新编号：原地继承资格，不会被当成“走了”"""
    gate, tr = new_gate(), SimpleTracker()
    walk = [[(200 + i * 50, 600)] for i in range(16)]
    (_, people, _), t = run(gate, tr, walk + [[(1000, 600)]] * 30)
    assert len(people) == 1
    old = people[0].tid
    (_, _, _), t = run(gate, tr, [[]] * 6, t0=t)                       # 漏检 6 秒，跟踪器丢掉目标
    (_, people, _), _ = run(gate, tr, [[(1010, 610)]] * 10, t0=t)
    assert len(people) == 1 and people[0].tid != old


def test_desk_worker_cell_not_blacklisted():
    """在书桌前坐 6 小时（走过来的）：座位不会被当成家具格"""
    gate, tr = new_gate(), SimpleTracker()
    walk = [[(200 + i * 50, 600)] for i in range(16)]
    (_, people, _), _ = run(gate, tr, walk + [[(1000, 600)]] * 2000, dt=10)
    assert len(people) == 1 and not gate.furniture((1000, 600), W, H)


# ---------------- 进出通报 ----------------

def make_butler(tmp_path):
    b = Butler(events_path=str(tmp_path / 'alerts.jsonl'))
    b.sent = []
    real = b.notify
    b.notify = lambda level, text, event_type=None, cooldown=None, snapshot=None, clip=None: (
        b.sent.append((event_type, text, snapshot)), real(level, text, event_type, cooldown, snapshot, clip))[1]
    return b


def test_brief_miss_is_not_departure(tmp_path):
    b = make_butler(tmp_path)
    ev = lambda kind: 'snap_%s.jpg' % kind
    for t in range(0, 300, 1):
        b.update_presence(True, count=1, now=1000 + t, evidence=ev)
    for t in range(300, 330):                      # 30 秒没看到（遮挡）
        b.update_presence(False, now=1000 + t, evidence=ev)
    for t in range(330, 400):
        b.update_presence(True, count=1, now=1000 + t, evidence=ev)
    assert not [s for s in b.sent if s[0] in ('departure', 'arrival')]


def test_departure_and_arrival_with_snapshot(tmp_path):
    b = make_butler(tmp_path)
    ev = lambda kind: 'snap_%s.jpg' % kind
    for t in range(0, 300):
        b.update_presence(True, count=1, now=1000 + t, evidence=ev)
    for t in range(300, 400):
        b.update_presence(False, now=1000 + t, evidence=ev)
    dep = [s for s in b.sent if s[0] == 'departure']
    assert len(dep) == 1 and dep[0][2] == 'snap_departure.jpg'
    for t in range(400, 410):
        b.update_presence(True, count=2, now=1000 + t, evidence=ev)
    arr = [s for s in b.sent if s[0] == 'arrival']
    assert len(arr) == 1 and '2 人' in arr[0][1] and arr[0][2] == 'snap_arrival.jpg'
    assert b.mode == HOME


def test_intermittent_return_switches_home(tmp_path):
    """回家的人在各房间走动、时进时出镜头：每次只露面 40 秒，中间离开 70 秒，也要切回在家"""
    b = make_butler(tmp_path)
    b.mode = AWAY
    t = 1000
    for _ in range(4):
        for _ in range(40):
            b.update_presence(True, count=1, now=t)
            t += 1
        for _ in range(70):
            b.update_presence(False, now=t)
            t += 1
    assert b.mode == HOME


def test_brief_pass_in_away_stays_away(tmp_path):
    """无人值守时只出现 1 分钟，不切回在家（入侵照常报）"""
    b = make_butler(tmp_path)
    b.mode = AWAY
    for t in range(60):
        b.update_presence(True, count=1, now=1000 + t)
    for t in range(60, 400):
        b.update_presence(False, now=1000 + t)
    assert b.mode == AWAY


def test_manual_mode_and_mute_survive_restart(tmp_path):
    b = make_butler(tmp_path)
    b.handle_command('是我')
    b.handle_command('静音2小时')
    b2 = Butler(events_path=str(tmp_path / 'alerts.jsonl'))
    assert b2.mode == HOME and b2.manual_until > b2.muted_until - 3 * 3600
    assert b2.muted_until > 0


def test_left_into_other_room_is_not_departure(tmp_path):
    """标了大门：人最后不是从大门消失 → 不报出门，10 分钟后仍在家；够 AWAY_INSIDE_SEC 才进无人值守"""
    b = make_butler(tmp_path)
    for t in range(0, 300):
        b.update_presence(True, count=1, now=1000 + t)
    for t in range(300, 300 + 1200):
        b.update_presence(False, now=1000 + t, exit_via_entry=False)
    assert not [s for s in b.sent if s[0] == 'departure'] and b.mode == HOME
    for t in range(1500, 300 + 3700):
        b.update_presence(False, now=1000 + t, exit_via_entry=False)
    assert b.mode == AWAY


def test_left_via_door_is_departure(tmp_path):
    b = make_butler(tmp_path)
    for t in range(0, 300):
        b.update_presence(True, count=1, now=1000 + t)
    for t in range(300, 300 + 700):
        b.update_presence(False, now=1000 + t, exit_via_entry=True)
    assert [s for s in b.sent if s[0] == 'departure'] and b.mode == AWAY


# ---------------- 整条感知链：雕像不再拖住警觉等级和算力 ----------------

class FakeModel:
    def __init__(self, dets):
        self.dets, self.calls = dets, 0

    def __call__(self, frame, th=0.35):
        self.calls += 1
        return [dict(d) for d in self.dets(self.calls)]


def test_statue_scene_stays_idle_and_skips_pose(tmp_path):
    from mibutler.app import Ctx
    from mibutler.brain.gate import MotionGate, AlertLevel, IDLE
    from mibutler.duties import sense

    frame = np.zeros((H, W, 3), np.uint8)
    seq = [0]

    def latest():
        seq[0] += 1
        return frame, float(seq[0]), seq[0]

    statue = lambda n: [dict(box(1200 + (n % 3) * 10, 600), kpts=KPTS)]
    det, pose = FakeModel(statue), FakeModel(statue)
    mob = MobilityFilter()
    app = SimpleNamespace(
        frames=SimpleNamespace(latest=latest), det=det, pose=pose,
        tracker=SimpleTracker(max_misses=5), pet_tracker=SimpleTracker(max_misses=10),
        mobility=mob, persons=PersonGate(mob), butler=make_butler(tmp_path),
        gate=MotionGate(), level=AlertLevel(hold=30),
        incidents=SimpleNamespace(highest_open=lambda: 0),
        ptz=SimpleNamespace(zones_valid=True), view=SimpleNamespace(aligned=True),
        in_rest=lambda pt: False, presence_evidence=lambda kind: None,
        household=SimpleNamespace(maybe_reload=lambda: None, static_at=lambda pt, W, H: None))
    c = Ctx({})
    import time as _t
    base = _t.time()
    for i in range(200):
        sense.detect(app, c)
        app.level.since = base - 100      # 跳过降级迟滞
        app.level.update(base + i, False, bool(c.active), False)
    assert c.persons and not c.active and not c.people
    assert app.level.level == IDLE
    assert pose.calls == 0, '只有静物时不该跑姿态模型'
    assert c.mode == HOME


# ---------------- 指令入口鉴权 ----------------

def test_command_endpoint_requires_token(tmp_path, monkeypatch):
    import json as _j, urllib.request, urllib.error
    import mibutler.butler as bm
    monkeypatch.setenv('COMMAND_TOKEN', 'secret-x')
    b = Butler(events_path=str(tmp_path / 'alerts.jsonl'))
    srv = b.start_command_server(port=18766)
    assert srv is not None

    def post(headers, url='http://127.0.0.1:18766/command'):
        req = urllib.request.Request(url, data='静音5'.encode(), headers=headers)
        try:
            return urllib.request.urlopen(req, timeout=3).status
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert post({}) == 403                                             # 没口令
        assert post({'X-Butler-Token': 'secret-x', 'Origin': 'http://evil.com'}) == 403   # 网页发起
        assert post({'X-Butler-Token': 'secret-x', 'Host': 'evil.com:18766'}) == 403      # DNS 重绑定
        assert b.muted_until == 0
        assert post({'X-Butler-Token': 'secret-x'}) == 200
        assert b.muted_until > 0
        try:
            urllib.request.urlopen('http://127.0.0.1:18766/command?text=%E6%98%AF%E6%88%91', timeout=3)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 405                                           # GET 不再执行指令
    finally:
        srv.shutdown()
