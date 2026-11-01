# -*- coding: utf-8 -*-
"""越用越准：事后补标、记录漏报、家庭知识

主人（或替主人复盘的 AI，经主人同意后）的纠正都落到 events/ 下，执事和 home-review skill 共用：

  labels.jsonl     每条纠正：{ts, kind: false|true|missed, incident, type, opened, note, by}
                   false → 写入误报学习（feedback.jsonl，同位置同类型 14 天内 3 次降一级）+ 跌倒样本标 0
                   true  → 撤销此前对这件事的误报记录 + 跌倒样本标 1
                   missed → 只记录（时间、描述），每周复盘时统计
  household.json   家庭知识：静物位置、镜头覆盖、作息、光照规律、其他事实；执事每分钟检查一次是否有更新

安全底线：学习只会让提醒降级，不会让它消失；火情永远不降级（见 incidents.NEVER_DOWNGRADE）；
静物区只屏蔽“没走过路”的人形，真人走进去照样算人。
"""
import glob, json, os, re, time

KIND_CN = {'false': '误报', 'true': '属实', 'missed': '漏报'}


# ---------------- 读写小工具 ----------------

def read_jsonl(path):
    out = []
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def append_jsonl(path, rec):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _write_jsonl(path, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, path)


# ---------------- 找事件 ----------------

TIME_RE = re.compile(r'^(?:(\d{4}-?\d{2}-?\d{2})\s+)?(\d{1,2})[:：](\d{2})$')


def parse_ref(ref):
    """'09:30' / '2026-01-01 09:30' → ('time', 'YYYY-MM-DD', 'HH:MM')；'#7' → ('subject', 7)；
    其他（如 '102900-3'）→ ('id', ref)"""
    ref = (ref or '').strip()
    m = TIME_RE.match(ref)
    if m:
        d, h, mi = m.groups()
        if d:
            d = d.replace('-', '')
            d = '%s-%s-%s' % (d[:4], d[4:6], d[6:8])
        return ('time', d or time.strftime('%Y-%m-%d'), '%02d:%s' % (int(h), mi))
    if ref.startswith('#') and ref[1:].isdigit():
        return ('subject', int(ref[1:]))
    return ('id', ref.lstrip('#'))


def _matches(rec, ref):
    kind = ref[0]
    if kind == 'id':
        return rec.get('id') == ref[1]
    if kind == 'subject':
        return rec.get('subject') == ref[1]
    _, day, hm = ref
    opened = rec.get('opened', '')
    if not opened.startswith(day):
        return False
    t0 = time.mktime(time.strptime(opened, '%Y-%m-%d %H:%M:%S'))
    t = time.mktime(time.strptime('%s %s:00' % (day, hm), '%Y-%m-%d %H:%M:%S'))
    return t0 - 120 <= t <= t0 + max(rec.get('duration_sec', 0), 0) + 120


def find_closed(events_dir, ref):
    """在 incidents.jsonl（含轮转归档）里找；按时间引用时只找当天，按编号 / #目标 时取最近的"""
    rows = []
    for p in sorted(glob.glob(os.path.join(events_dir, 'incidents*.jsonl'))):
        rows += read_jsonl(p)
    r = parse_ref(ref) if isinstance(ref, str) else ref
    hits = [x for x in rows if _matches(x, r)]
    return sorted(hits, key=lambda x: x.get('opened', ''))


# ---------------- 补标 ----------------

def _label_samples(events_dir, incident_id, value):
    n = 0
    for path in glob.glob(os.path.join(events_dir, 'samples', 'fall_*_%s.json' % incident_id)):
        try:
            meta = json.load(open(path, encoding='utf-8'))
            meta['label'] = value
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
            n += 1
        except Exception as e:
            print('[learning] 样本标注失败：', e)
    return n


def label(events_dir, ref, kind, manager=None, note='', by='owner', now=None):
    """给已结案（或仍在进行）的事件补标。kind: 'false' 误报 / 'true' 属实。
    manager：运行中的 IncidentManager，传入时立即生效；不传（执事没在跑）则只写文件，下次启动生效。
    返回 (处理了几件, 说明文字)"""
    now = now or time.time()
    r = parse_ref(ref)
    # 1. 还没结案的：走原来的 ack 流程
    if manager is not None:
        n = manager.ack_matching(lambda inc: _matches(inc.to_dict(now), r), kind)
        if n:
            return n, '已标为%s（进行中的事件）。' % KIND_CN[kind]
    # 2. 已结案的
    hits = find_closed(events_dir, r)
    if not hits:
        return 0, '没找到 %s 对应的事件。可以说时间（如 09:30）、目标编号（如 #7）或事件编号。' % ref
    if r[0] != 'time':
        hits = hits[-1:]                # 编号可能跨天重复，只取最近一次
    fb_path = os.path.join(events_dir, 'feedback.jsonl')
    for inc in hits:
        append_jsonl(os.path.join(events_dir, 'labels.jsonl'), {
            'ts': now, 'kind': kind, 'incident': inc.get('id'), 'types': inc.get('types'),
            'opened': inc.get('opened'), 'note': note, 'by': by})
        if kind == 'false':
            for t in set(inc.get('types') or []):
                for c in (inc.get('cells') or [None]):
                    rec = {'ts': now, 'type': t, 'cell': c, 'incident': inc.get('id')}
                    append_jsonl(fb_path, rec)
                    if manager is not None:
                        manager.feedback.append(rec)
        else:   # 属实：撤销之前对这件事的误报记录
            keep = [x for x in read_jsonl(fb_path) if x.get('incident') != inc.get('id')]
            if os.path.exists(fb_path):
                _write_jsonl(fb_path, keep)
            if manager is not None:
                manager.feedback[:] = [x for x in manager.feedback if x.get('incident') != inc.get('id')]
        if inc.get('family') == 'person_down':
            _label_samples(events_dir, inc.get('id'), 1 if kind == 'true' else 0)
    desc = '、'.join('%s %s' % (h.get('opened', '')[11:16], '/'.join(h.get('types') or [])) for h in hits)
    return len(hits), '已把 %s 标为%s。' % (desc, KIND_CN[kind]) + (
        '同一位置误报多了，以后在这里会降一级提醒。' if kind == 'false' else '')


def missed(events_dir, when, what, by='owner', now=None):
    """记录漏报：when 如 '15:00' 或 '15:00-15:30'，what 是描述"""
    now = now or time.time()
    append_jsonl(os.path.join(events_dir, 'labels.jsonl'), {
        'ts': now, 'kind': 'missed', 'day': time.strftime('%Y-%m-%d', time.localtime(now)),
        'when': when, 'note': what, 'by': by})
    return '记下了：%s %s 没有提醒。每周复盘时我会一起看为什么漏了。' % (when, what)


LABEL_CMD = re.compile(r'^(没事|误报|虚惊|属实|真的|漏报)\s*(.+)$')


def parse_label_command(text):
    """'没事 09:30' / '属实 #7' / '漏报 15:00 有人进来没报' → (kind, ref, note)；不是补标指令返回 None"""
    m = LABEL_CMD.match((text or '').strip())
    if not m:
        return None
    word, rest = m.groups()
    if word == '漏报':
        parts = rest.split(None, 1)
        if not re.search(r'\d', parts[0]):
            return None
        return 'missed', parts[0], parts[1] if len(parts) > 1 else ''
    kind = 'true' if word in ('属实', '真的') else 'false'
    parts = rest.split(None, 1)
    if not re.search(r'\d', parts[0]):     # “没事了”“真的吗”不是补标：后面得跟时间、#目标或事件编号
        return None
    return kind, parts[0], parts[1] if len(parts) > 1 else ''


def apply_label_command(events_dir, text, manager=None, by='owner'):
    p = parse_label_command(text)
    if not p:
        return None
    kind, ref, note = p
    if kind == 'missed':
        return missed(events_dir, ref, note, by=by)
    return label(events_dir, ref, kind, manager=manager, note=note, by=by)[1]


# ---------------- 家庭知识 ----------------

EMPTY_HOUSEHOLD = {'static_objects': [], 'camera': {}, 'routine': {}, 'lighting': [], 'facts': [],
                   'changelog': []}


class Household:
    """events/household.json 的读取与热更新（执事侧只用 static_objects）"""

    def __init__(self, path):
        self.path = path
        self.data = dict(EMPTY_HOUSEHOLD)
        self._mtime = None
        self._checked = 0.0
        self.reload()

    def reload(self):
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            self.data, self._mtime = dict(EMPTY_HOUSEHOLD), None
            return False
        if mt == self._mtime:
            return False
        try:
            d = json.load(open(self.path, encoding='utf-8'))
            self.data = {**EMPTY_HOUSEHOLD, **d}
            self._mtime = mt
            print('[household] 已载入家庭知识：静物 %d 处，事实 %d 条'
                  % (len(self.data['static_objects']), len(self.data['facts'])))
            return True
        except Exception as e:
            print('[household] household.json 读取失败，沿用上一版：', e)
            return False

    def maybe_reload(self, now=None):
        now = now or time.time()
        if now - self._checked > 60:
            self._checked = now
            self.reload()

    def static_at(self, pt, W, H):
        """这个位置是不是主人标过的静物（雕像、挂衣、人形立牌……）"""
        x, y = pt[0] / max(W, 1), pt[1] / max(H, 1)
        for o in self.data.get('static_objects') or []:
            r = o.get('roi')
            if r and len(r) == 4 and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return o.get('name') or True
        return None
