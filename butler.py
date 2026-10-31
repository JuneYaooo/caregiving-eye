# -*- coding: utf-8 -*-
"""米执事本体: 值班模式引擎 + 通报话术 + 推送 + 指令
被 care_v2 等感知层调用; 也可独立 import 用于发日报/接收指令。
"""
import json, os, time, threading
import urllib.request
from common_config import cfg

HERE = os.path.dirname(os.path.abspath(__file__))

# 值班模式
AWAY, HOME, PET, NIGHT = 'away', 'home', 'pet', 'night'
MODE_CN = {AWAY: '无人值守', HOME: '在家照护', PET: '宠物模式', NIGHT: '夜间加强'}

LEVEL_TAG = {
    'CRITICAL': '〔急报〕', 'GUARD': '〔值守〕', 'PET': '〔宠物〕',
    'INFO': '〔执事〕', 'MORNING': '〔晨报〕',
}

# 各类事件最小通报间隔(秒), 防轰炸
DEFAULT_COOLDOWN = {
    'fall': 60, 'lying_long': 300, 'intrusion': 120, 'fire': 30,
    'zone_dwell': 300, 'night_wander': 300, 'absence': 1800,
    'cat_zone': 120, 'mode_change': 0, 'audio': 60,
}


class Butler:
    def __init__(self, events_path=None):
        self.name = cfg('BUTLER_NAME', '米执事')
        self.owner = cfg('OWNER_TITLE', '老爷')
        self.webhook = cfg('ALERT_WEBHOOK', '')
        self.events_path = events_path or os.path.join(HERE, 'events', 'alerts.jsonl')
        os.makedirs(os.path.dirname(self.events_path), exist_ok=True)
        self.mode = HOME
        self.manual_until = 0            # 指令覆盖模式的截止时间
        self.muted_until = 0             # 静音 (仅压制非 CRITICAL)
        self.last_sent = {}              # event_type -> ts
        self.person_since = None         # 有人出现的起始时间
        self.person_absent_since = None
        self.schedule = self._parse_schedule(cfg('MODE_SCHEDULE', ''))

    # ---------- 值班制度 ----------
    @staticmethod
    def _parse_schedule(text):
        """'23:00-07:00:night;08:00-09:00:away' -> [(start_min,end_min,mode)]"""
        out = []
        for seg in (text or '').split(';'):
            seg = seg.strip()
            if not seg or ':' not in seg:
                continue
            window, mode = seg.rsplit(':', 1)
            try:
                a, b = window.split('-')
                ah, am = map(int, a.split(':'))
                bh, bm = map(int, b.split(':'))
                out.append((ah * 60 + am, bh * 60 + bm, mode))
            except ValueError:
                continue
        return out

    def _scheduled_mode(self, now=None):
        if not self.schedule:
            return None
        t = time.localtime(now or time.time())
        cur = t.tm_hour * 60 + t.tm_min
        for a, b, mode in self.schedule:
            if a <= cur < b:
                return mode
            if a > b and (cur >= a or cur < b):  # 跨午夜
                return mode
        return None

    def update_presence(self, person_present, only_pets=False, now=None):
        """感知层每帧调用: 依据在场情况自动切模式 (带迟滞)"""
        now = now or time.time()
        if now < self.manual_until:
            return self.mode
        sched = self._scheduled_mode(now)
        if sched and person_present is False:
            self.set_mode(sched, '按日程')
            return self.mode
        if person_present:
            if self.person_since is None:
                self.person_since = now
            if self.person_absent_since is not None:
                self.person_absent_since = None
            # 在家超过迟滞期 → HOME
            if now - self.person_since > 120 and self.mode in (AWAY, PET):
                self.set_mode(HOME, '%s出现超过2分钟' % self.owner)
        else:
            if self.person_since is not None:
                self.person_absent_since = now
                self.person_since = None
            if self.person_absent_since and now - self.person_absent_since > 600:
                if only_pets and self.mode != PET:
                    self.set_mode(PET, '家中只剩宠物')
                elif self.mode != AWAY:
                    self.set_mode(AWAY, '家中无人超过10分钟')
        return self.mode

    def set_mode(self, mode, reason=''):
        if mode == self.mode:
            return self.mode
        old, self.mode = self.mode, mode
        self.notify('INFO', '切换到%s（%s）。有事我叫%s。' % (MODE_CN[mode], reason, self.owner),
                    event_type='mode_change', cooldown=0)
        return self.mode

    def set_manual_mode(self, mode, hours=4):
        self.manual_until = time.time() + hours * 3600
        self.set_mode(mode, '%s指令' % self.owner)

    # ---------- 通报 ----------
    def notify(self, level, text, event_type=None, cooldown=None, snapshot=None):
        """按执事口吻发通报; 返回 True=真的发了"""
        now = time.time()
        et = event_type or level
        cd = DEFAULT_COOLDOWN.get(et, 60) if cooldown is None else cooldown
        if level != 'CRITICAL' and now < self.muted_until:
            return False
        if cd and now - self.last_sent.get(et, 0) < cd:
            return False
        self.last_sent[et] = now
        tag = LEVEL_TAG.get(level, '〔执事〕')
        full = '%s %s' % (tag, text)
        if level == 'CRITICAL':
            full += ' 请立即查看！回复"没事"解除。'
        print('[%s][%s] %s' % (time.strftime('%H:%M:%S'), MODE_CN[self.mode], full))
        # 落盘
        rec = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'level': level,
               'mode': self.mode, 'type': et, 'text': full,
               'snapshot': snapshot}
        with open(self.events_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        # 推送
        if self.webhook:
            try:
                body = json.dumps({'msgtype': 'text',
                                   'text': {'content': full}}).encode()
                req = urllib.request.Request(self.webhook, data=body,
                                             headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=5)
            except Exception as e:
                print('webhook fail:', e)
        return True

    # ---------- 指令 ----------
    def handle_command(self, text):
        """返回执事的回应文本; None=非指令"""
        t = (text or '').strip()
        if t in ('是我', '是我本人'):
            self.set_manual_mode(HOME, hours=2)
            return '欢迎回家，%s。切换在家照护。' % self.owner
        if t in ('没事', '没事了', '虚惊'):
            return '好的，%s，我记下本次为虚惊。' % self.owner
        if t.startswith('静音'):
            hours = 1
            try:
                hours = float(t.replace('静音', '').replace('小时', '').strip() or 1)
            except ValueError:
                pass
            self.muted_until = time.time() + hours * 3600
            return '静音 %g 小时（急报除外）。' % hours
        if t.startswith('模式'):
            m = t.replace('模式', '').strip().lower()
            if m in (AWAY, HOME, PET, NIGHT):
                self.set_manual_mode(m, hours=4)
                return '已切换%s，4小时内不自动改。' % MODE_CN[m]
            return '可用模式: away/home/pet/night'
        if t == '日报':
            from report import daily_report
            return daily_report()
        return None


if __name__ == '__main__':
    b = Butler()
    b.notify('INFO', '%s上线，当前模式：%s。' % (b.name, MODE_CN[b.mode]))
    print(b.handle_command('静音2小时'))
    print(b.handle_command('模式 away'))
    b.notify('GUARD', '客厅出现移动目标，已转向跟踪。', event_type='intrusion')
    print('butler OK')
