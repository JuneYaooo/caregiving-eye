# -*- coding: utf-8 -*-
"""米执事本体: 值班模式引擎 + 通报话术 + 推送 + 指令
由 app.py 组装、duties/ 里的任务通过研判层调用；也可独立 import 用于发日报 / 接收指令。
"""
import json, os, queue, time, threading
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from mibutler.config import cfg, ROOT


# 值班模式
AWAY, HOME, PET, NIGHT = 'away', 'home', 'pet', 'night'
MODE_CN = {AWAY: '无人值守', HOME: '在家照护', PET: '宠物模式', NIGHT: '夜间加强'}

LEVEL_TAG = {
    'CRITICAL': '〔急报〕', 'GUARD': '〔值守〕', 'PET': '〔宠物〕',
    'INFO': '〔执事〕', 'MORNING': '〔晨报〕',
}

# 各类事件最小通报间隔(秒), 防轰炸
DEFAULT_COOLDOWN = {
    # 无人值守
    'intrusion': 120, 'fire': 30, 'fire_suspect': 120, 'fire_alarm_sound': 30,
    'glass_break': 60, 'absence': 1800,
    # 在家照护
    'fall': 60, 'lying_long': 300, 'illness': 300, 'inactive': 1800,
    'zone_dwell': 300, 'night_wander': 300, 'fall_suspect': 60,
    'routine_late': 6 * 3600, 'routine_low': 6 * 3600, 'routine_night': 6 * 3600,
    'child_danger': 60, 'child_danger_watched': 300, 'child_alone': 900, 'baby_cry': 300,
    # 宠物
    'cat_zone': 120, 'pet_distress': 300, 'pet_unseen': 3600,
    'mode_change': 0, 'view_shift': 3600, 'supervisor': 0,
}


class Butler:
    def __init__(self, events_path=None):
        self.name = cfg('BUTLER_NAME', '米执事')
        self.owner = cfg('OWNER_TITLE', '老爷')
        self.webhook = cfg('ALERT_WEBHOOK', '')
        self.events_path = events_path or os.path.join(ROOT, 'events', 'alerts.jsonl')
        os.makedirs(os.path.dirname(self.events_path), exist_ok=True)
        self.mode = HOME
        self.manual_until = 0            # 指令覆盖模式的截止时间
        self.muted_until = 0             # 静音 (仅压制非 CRITICAL)
        self.last_sent = {}              # event_type -> ts
        self.person_since = None         # 有人出现的起始时间
        self.away_after_sec = float(cfg('AWAY_DELAY_SEC', 600))  # 无人多久进入 AWAY
        self.person_absent_since = None
        self.schedule = self._parse_schedule(cfg('MODE_SCHEDULE', ''))
        self._lock = threading.Lock()    # 多个线程可能同时通报
        self.incidents = None            # 由大脑注入：事件研判层
        self.on_command = None           # 由大脑注入：处理“状态 / 看看”等需要调度的指令
        # 推送放到后台线程，网络卡顿不会拖慢识别
        self._outbox = queue.Queue(maxsize=200)
        if self.webhook:
            threading.Thread(target=self._sender, daemon=True, name='webhook').start()

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
            mode = mode.strip().lower()
            if mode not in MODE_CN:
                print('[butler] MODE_SCHEDULE 里的模式 "%s" 不认识（可用 away/home/pet/night），已忽略这一段' % mode)
                continue
            try:
                a, b = window.split('-')
                ah, am = map(int, a.split(':'))
                bh, bm = map(int, b.split(':'))
                out.append((ah * 60 + am, bh * 60 + bm, mode))
            except ValueError:
                print('[butler] MODE_SCHEDULE 时段 "%s" 格式不对（应为 HH:MM-HH:MM），已忽略' % window)
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

    def update_presence(self, person_present, only_pets=False, count=0, now=None):
        """感知层每帧调用: 依据在场情况自动切模式 (带迟滞) + 进出事件"""
        now = now or time.time()
        # 先记录在场 / 离场时间，指令和日程生效期间也要记，否则结束后无法判断离家多久
        if person_present:
            first_back = self.person_since is None and self.person_absent_since is not None
            gap = (now - self.person_absent_since) if self.person_absent_since else 0
            self.person_since = self.person_since or now
            self.person_absent_since = None
            # 进出事件: 空置超过60秒后有人出现 → 有人回来了 (无人值守模式交给入侵告警)
            if first_back and gap > 60 and self.mode != AWAY:
                self.notify('INFO', '有人回来了，当前在家 %d 人。' % max(count, 1),
                            event_type='arrival', cooldown=0)
        else:
            just_left = self.person_since is not None
            spell = (now - self.person_since) if self.person_since else 0
            self.person_absent_since = self.person_absent_since or now
            self.person_since = None
            # 进出事件: 在场持续超过2分钟后人全离开 → 通报离家
            if just_left and spell > 120:
                self.notify('INFO', '人都出门了，%g 分钟后进入无人值守。'
                            % (self.away_after_sec / 60), event_type='departure')
        if now < self.manual_until:
            return self.mode
        sched = self._scheduled_mode(now)
        if sched and not person_present:
            self.set_mode(sched, '按日程')
            return self.mode
        if self.mode == NIGHT and sched != NIGHT:
            # 夜间日程结束：回到在家照护；离场计时从现在重新算，免得一早就判成无人
            self.set_mode(HOME, '夜间日程结束')
            self.person_absent_since = None if person_present else now
        if person_present:
            # 在家超过迟滞期 → HOME
            if now - self.person_since > 120 and self.mode in (AWAY, PET):
                self.set_mode(HOME, '%s出现超过2分钟' % self.owner)
        elif now - self.person_absent_since > self.away_after_sec:
            if only_pets and self.mode != PET:
                self.set_mode(PET, '家中只剩宠物')
            elif not only_pets and self.mode != AWAY:
                self.set_mode(AWAY, '家中无人超过%g分钟' % (self.away_after_sec / 60))
        return self.mode

    def set_mode(self, mode, reason=''):
        if mode == self.mode:
            return self.mode
        old, self.mode = self.mode, mode
        self.notify('INFO', '切换到%s（%s）。有事我叫%s。' % (MODE_CN.get(mode, mode), reason, self.owner),
                    event_type='mode_change', cooldown=0)
        return self.mode

    def set_manual_mode(self, mode, hours=4):
        self.manual_until = time.time() + hours * 3600
        self.set_mode(mode, '%s指令' % self.owner)

    # ---------- 通报 ----------
    def notify(self, level, text, event_type=None, cooldown=None, snapshot=None, clip=None):
        """按执事口吻发通报; 返回 True=真的发了"""
        with self._lock:
            return self._notify(level, text, event_type, cooldown, snapshot, clip)

    def _notify(self, level, text, event_type, cooldown, snapshot, clip):
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
        print('[%s][%s] %s' % (time.strftime('%H:%M:%S'), MODE_CN.get(self.mode, self.mode), full))
        # 落盘
        rec = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'level': level,
               'mode': self.mode, 'type': et, 'text': full,
               'snapshot': snapshot, 'clip': clip}
        with open(self.events_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        # 推送（异步）
        if self.webhook:
            try:
                self._outbox.put_nowait(full)
            except queue.Full:
                print('webhook 队列已满，丢弃一条')
        return True

    def _sender(self):
        while True:
            text = self._outbox.get()
            for attempt in range(3):
                try:
                    body = json.dumps({'msgtype': 'text', 'text': {'content': text}}).encode()
                    req = urllib.request.Request(self.webhook, data=body,
                                                 headers={'Content-Type': 'application/json'})
                    urllib.request.urlopen(req, timeout=5)
                    break
                except Exception as e:
                    print('webhook fail (%d/3):' % (attempt + 1), e)
                    time.sleep(2 ** attempt)

    # ---------- 指令 ----------
    def handle_command(self, text):
        """返回执事的回应文本; None=非指令"""
        t = (text or '').strip()
        if t in ('是我', '是我本人'):
            if self.incidents:
                self.incidents.ack('me', families={'security'})
            self.set_manual_mode(HOME, hours=2)
            return '欢迎回家，%s。切换在家照护。' % self.owner
        if t in ('没事', '没事了', '虚惊', '误报'):
            n = self.incidents.ack('false') if self.incidents else 0
            return ('好的，%s，记为误报。同一位置多次误报后我会降低这里的提醒级别。' % self.owner
                    if n else '好的，%s，目前没有待确认的事件。' % self.owner)
        if t in ('属实', '真的', '确实摔了', '真摔了'):
            n = self.incidents.ack('true') if self.incidents else 0
            return ('好的，%s，记为真实事件，已停止催促。' % self.owner if n
                    else '目前没有待确认的事件。')
        if t in ('收到', '我去看', '知道了'):
            n = self.incidents.ack('ok') if self.incidents else 0
            return '好的，不再催促。' if n else '目前没有待确认的事件。'
        if t in ('状态', '看看', '看看客厅') and self.on_command:
            return self.on_command(t)
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
            from mibutler.report import daily_report
            return daily_report(events_dir=os.path.dirname(self.events_path))
        return None

    # ---------- 指令入口（本机 HTTP）----------
    def start_command_server(self, port=None, host='127.0.0.1'):
        """在本机开一个很小的 HTTP 入口，机器人或脚本把主人的回复转进来：
             curl -d "没事" http://127.0.0.1:8766/command
             python main.py cmd 没事
           只监听 127.0.0.1，不对局域网开放。COMMAND_PORT=0 关闭。"""
        port = int(cfg('COMMAND_PORT', 8766) if port is None else port)
        if port <= 0:
            return None
        butler = self

        class H(BaseHTTPRequestHandler):
            def _reply(self, code, text):
                body = text.encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, text):
                if not self.path.startswith('/command'):
                    return self._reply(404, 'not found')
                try:
                    r = butler.handle_command(text)
                except Exception as e:
                    return self._reply(500, '指令出错：%s' % e)
                self._reply(200, r if r is not None else '没听懂这条指令。')

            def do_GET(self):
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                self._handle((q.get('text') or [''])[0])

            def do_POST(self):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(min(n, 4096)).decode('utf-8', 'replace')
                if raw.lstrip().startswith('{'):
                    try:
                        raw = json.loads(raw).get('text', '')
                    except ValueError:
                        pass
                self._handle(raw)

            def log_message(self, *a):
                pass

        try:
            srv = ThreadingHTTPServer((host, port), H)
        except OSError as e:
            print('[butler] 指令入口启动失败（端口 %d）：%s' % (port, e))
            return None
        threading.Thread(target=srv.serve_forever, daemon=True, name='commands').start()
        print('[butler] 指令入口：http://%s:%d/command' % (host, port))
        return srv


def send_command(text, port=None):
    """把一条指令发给正在运行的执事，返回执事的回答"""
    port = int(port or cfg('COMMAND_PORT', 8766))
    req = urllib.request.Request('http://127.0.0.1:%d/command' % port, data=text.encode('utf-8'),
                                 headers={'Content-Type': 'text/plain; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode('utf-8')
