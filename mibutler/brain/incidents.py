# -*- coding: utf-8 -*-
"""事件研判：把零散信号合并成“一件事”，决定提不提醒、要不要升级、何时结案

各项能力只上报信号（signal），不直接发通知。研判层负责：
  合并   同一类、同一对象的信号归入同一个事件（例如同一个人的跌倒 → 久卧 → 不适）。
  升级   新信号级别更高才再次提醒，并标明“升级”；同级信号只记录不重复提醒。
  存证   只在事件新建或升级时抓拍 / 录像，避免同一件事写满磁盘。
  催办   急报没人确认时，每 REMIND_SEC 秒再提醒一次，最多 MAX_REMINDS 次。
  结案   一段时间没有新信号自动结案；急报要更久才自动结案并说明。
  学习   回复“没事”记为误报。同一类型在同一画面位置 14 天内误报 >= 3 次，以后在这里降一级提醒
         （火情类除外，永不降级）。回复“收到”只停止催办；回复“属实”记为真实事件。
         误报 / 属实还会通知 on_ack 回调，跌倒样本库据此给样本打标签，用于再训练骨架模型。
"""
import json, os, threading, time

RANK = {'INFO': 0, 'PET': 1, 'GUARD': 2, 'CRITICAL': 3}
FAMILY = {
    'fall': 'person_down', 'lying_long': 'person_down', 'illness': 'person_down',
    'inactive': 'person_down', 'zone_dwell': 'person_down',
    'fire': 'fire', 'fire_suspect': 'fire', 'fire_alarm_sound': 'fire',
    'intrusion': 'security', 'glass_break': 'security',
    'child_danger': 'child', 'child_danger_watched': 'child', 'child_alone': 'child',
    'baby_cry': 'baby', 'night_wander': 'night',
    'cat_zone': 'pet_zone', 'pet_distress': 'pet', 'pet_unseen': 'pet',
    'absence': 'absence', 'stream_down': 'system', 'view_shift': 'system',
    'fall_suspect': 'person_down',
    'routine_late': 'routine', 'routine_low': 'routine', 'routine_night': 'routine',
}
NEVER_DOWNGRADE = {'fire', 'fire_alarm_sound'}
FAMILY_CN = {'person_down': '有人倒地 / 不适', 'fire': '火情', 'security': '安全',
             'child': '儿童安全', 'pet_zone': '宠物禁区', 'pet': '宠物', 'system': '系统',
             'routine': '作息'}


class Incident:
    _n = 0

    def __init__(self, family, subject, level, now):
        Incident._n += 1
        self.id = '%s-%d' % (time.strftime('%H%M%S'), Incident._n)
        self.family, self.subject, self.level = family, subject, level
        self.opened = self.updated = self.last_notified = now
        self.types, self.texts, self.cells = [], [], set()
        self.snapshot = self.clip = None
        self.reminds = 0
        self.acked = None             # None | 'ok' | 'false' | 'me'

    def to_dict(self, now):
        return {'id': self.id, 'family': self.family, 'subject': self.subject,
                'level': self.level, 'types': self.types, 'texts': self.texts[-5:],
                'opened': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.opened)),
                'duration_sec': int(now - self.opened), 'acked': self.acked,
                'snapshot': self.snapshot, 'clip': self.clip, 'cells': sorted(self.cells)}


class IncidentManager:
    def __init__(self, butler, events_dir, remind_sec=180, max_reminds=5,
                 quiet_sec=120, critical_quiet_sec=1800):
        self.butler = butler
        self.remind_sec, self.max_reminds = remind_sec, max_reminds
        self.quiet_sec, self.critical_quiet_sec = quiet_sec, critical_quiet_sec
        self.open = {}                # (family, subject) -> Incident
        self.log_path = os.path.join(events_dir, 'incidents.jsonl')
        self.fb_path = os.path.join(events_dir, 'feedback.jsonl')
        self._lock = threading.RLock()
        self.feedback = self._load_feedback()
        self.on_ack = []              # 回调 fn(incident, kind)：主人确认后通知其他模块

    # ---------- 误报学习 ----------
    def _load_feedback(self):
        out = []
        if os.path.exists(self.fb_path):
            for line in open(self.fb_path, encoding='utf-8'):
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
        return out

    def _false_count(self, etype, cell, now):
        return sum(1 for f in self.feedback
                   if f['type'] == etype and f.get('cell') == cell and now - f['ts'] < 14 * 86400)

    def _record_false(self, inc, now):
        with open(self.fb_path, 'a', encoding='utf-8') as f:
            for t in set(inc.types):
                for c in (inc.cells or {None}):
                    rec = {'ts': now, 'type': t, 'cell': c, 'incident': inc.id}
                    self.feedback.append(rec)
                    f.write(json.dumps(rec) + '\n')

    # ---------- 信号入口 ----------
    def signal(self, etype, level, text, subject=None, cell=None, evidence=None, now=None):
        """evidence: callable(tag, level)->(snapshot, clip)，只在新建或升级时调用"""
        now = now or time.time()
        with self._lock:
            if etype not in NEVER_DOWNGRADE and cell and level in ('CRITICAL', 'GUARD') \
                    and self._false_count(etype, cell, now) >= 3:
                level = 'GUARD' if level == 'CRITICAL' else 'INFO'
                text += '（此处曾多次误报，已降一级）'
            family = FAMILY.get(etype, etype)
            key = (family, subject)
            inc = self.open.get(key)
            if inc and inc.acked in ('false', 'me'):
                inc.updated = now       # 已确认误报 / 本人：静默跟踪到结案
                return inc
            if inc is None:
                inc = Incident(family, subject, level, now)
                self.open[key] = inc
                notify, prefix = True, ''
            elif RANK[level] > RANK[inc.level]:
                inc.level = level
                notify, prefix = True, '【升级】'
            else:
                notify, prefix = False, ''
            inc.updated = now
            if etype not in inc.types:
                inc.types.append(etype)
            inc.texts.append(text)
            if cell:
                inc.cells.add(cell)
            if notify:
                if evidence and RANK[level] >= RANK['PET']:
                    try:
                        inc.snapshot, clip = evidence(etype, level)
                        inc.clip = clip or inc.clip
                    except Exception as e:
                        print('[incident] 存证失败：', e)
                self.butler.notify(level, prefix + text, event_type=etype, cooldown=0,
                                   snapshot=inc.snapshot, clip=inc.clip)
                inc.last_notified = now
            return inc

    # ---------- 周期：催办与结案 ----------
    def tick(self, now=None):
        now = now or time.time()
        with self._lock:
            for key, inc in list(self.open.items()):
                quiet = now - inc.updated
                if inc.level == 'CRITICAL' and inc.acked is None:
                    if now - inc.last_notified > self.remind_sec and inc.reminds < self.max_reminds:
                        inc.reminds += 1
                        inc.last_notified = now
                        self.butler.notify('CRITICAL', '【第 %d 次提醒，尚未确认】%s' % (inc.reminds, inc.texts[-1]),
                                           event_type=inc.types[-1], cooldown=0,
                                           snapshot=inc.snapshot, clip=inc.clip)
                    if quiet > self.critical_quiet_sec:
                        self._close(key, inc, now, '%d 分钟内没有再出现异常，自动结案；仍未确认，请查看录像。'
                                    % (quiet / 60))
                elif quiet > self.quiet_sec:
                    self._close(key, inc, now, None)

    def _close(self, key, inc, now, say):
        self.open.pop(key, None)
        if say:
            self.butler.notify('INFO', '%s：%s' % (FAMILY_CN.get(inc.family, inc.family), say),
                               event_type='incident_close', cooldown=0)
        with open(self.log_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(inc.to_dict(now), ensure_ascii=False) + '\n')

    # ---------- 主人回复 ----------
    def ack(self, kind, families=None):
        """kind: 'ok' 收到 / 'false' 误报 / 'true' 属实 / 'me' 是我本人；返回处理了几个事件"""
        now = time.time()
        with self._lock:
            # “属实”可以跟在“收到”之后补充
            ackable = (None, 'ok') if kind == 'true' else (None,)
            targets = [i for i in self.open.values() if i.acked in ackable
                       and (families is None or i.family in families)]
            if not targets:
                return 0
            if kind in ('ok', 'false', 'true'):
                # 主人回复的通常是最近收到的那条
                targets = [max(targets, key=lambda i: i.last_notified)]
            for inc in targets:
                inc.acked = kind
                if kind == 'false':
                    self._record_false(inc, now)
        self._fire_ack(targets, kind)
        return len(targets)

    def ack_matching(self, pred, kind):
        """按条件确认进行中的事件（事后补标“没事 10:29”时用）；返回处理了几个"""
        now = time.time()
        with self._lock:
            targets = [i for i in self.open.values() if pred(i)]
            for inc in targets:
                if kind == 'false' and inc.acked != 'false':
                    self._record_false(inc, now)
                inc.acked = kind
        self._fire_ack(targets, kind)
        return len(targets)

    def _fire_ack(self, targets, kind):
        for inc in targets:
            for fn in self.on_ack:
                try:
                    fn(inc, kind)
                except Exception as e:
                    print('[incident] on_ack 回调出错：', e)

    def highest_open(self):
        with self._lock:
            levels = [RANK[i.level] for i in self.open.values() if i.acked is None]
        return max(levels, default=-1)

    def snapshot(self, now=None):
        now = now or time.time()
        with self._lock:
            return [i.to_dict(now) for i in self.open.values()]
