# -*- coding: utf-8 -*-
"""米执事本体: 值班模式引擎 + 通报话术 + 推送 + 指令
由 app.py 组装、duties/ 里的任务通过研判层调用；也可独立 import 用于发日报 / 接收指令。
"""
import hmac, json, os, queue, secrets, time, threading
from collections import deque
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
        if self.webhook and not self.webhook.lower().startswith('https://'):
            print('[butler] 警告：ALERT_WEBHOOK 不是 https 地址，提醒内容会明文经过网络')
        self.events_path = events_path or os.path.join(ROOT, 'events', 'alerts.jsonl')
        os.makedirs(os.path.dirname(self.events_path), exist_ok=True)
        self.mode = HOME
        self.manual_until = 0            # 指令覆盖模式的截止时间
        self.muted_until = 0             # 静音 (仅压制非 CRITICAL)
        self.last_sent = {}              # event_type -> ts
        self.person_since = None         # 有人出现的起始时间
        self.away_after_sec = float(cfg('AWAY_DELAY_SEC', 600))  # 无人多久进入 AWAY
        self.person_absent_since = None
        self.depart_confirm_sec = float(cfg('DEPART_CONFIRM_SEC', 60))  # 连续多久没看到人才算离开
        # 标了大门（zones.json 的 entry_zones）时：人最后不是从大门消失的，多半去了镜头外的房间，
        # 不通报出门，等 AWAY_INSIDE_SEC 还没再出现才进入无人值守
        self.away_inside_sec = float(cfg('AWAY_INSIDE_SEC', 3600))
        self._exit_inside = False
        self._spell_start = None         # 本次在场的起点（短暂漏检后接着算）
        self._depart_pending = False
        # 回家确认：最近 HOME_WINDOW_SEC 秒里累计看到人满 HOME_CONFIRM_SEC 秒就切回在家。
        # 不要求连续：回家的人会在各房间走动，进出镜头，连续 2 分钟很难凑满。
        self.home_confirm_sec = float(cfg('HOME_CONFIRM_SEC', 120))
        self.home_window_sec = float(cfg('HOME_WINDOW_SEC', 600))
        self._seen = deque()             # [(ts, 这一次看到人计入的秒数)]
        self._last_update = None
        self.schedule = self._parse_schedule(cfg('MODE_SCHEDULE', ''))
        self._lock = threading.Lock()    # 多个线程可能同时通报
        self.state_path = os.path.join(os.path.dirname(self.events_path), 'butler_state.json')
        self._load_state()
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

    def update_presence(self, person_present, only_pets=False, count=0, now=None, evidence=None,
                        exit_via_entry=None):
        """感知层每次检测后调用: 依据在场情况自动切模式 (带迟滞) + 进出事件
        evidence(kind) -> 快照路径，只在真要通报进出时才调用（kind = 'arrival' / 'departure'）
        exit_via_entry: 看不到人时，最后一次是不是在大门区域看到的；None = 没标大门 / 不确定"""
        now = now or time.time()
        snap = lambda kind: evidence(kind) if evidence else None
        if person_present:
            dt = min(now - self._last_update, 2.0) if self._last_update else 0.0
            self._seen.append((now, dt))
        self._last_update = now
        while self._seen and now - self._seen[0][0] > self.home_window_sec:
            self._seen.popleft()
        # 先记录在场 / 离场时间，指令和日程生效期间也要记，否则结束后无法判断离家多久
        if person_present:
            first_back = self.person_since is None and self.person_absent_since is not None
            gap = (now - self.person_absent_since) if self.person_absent_since else 0
            if first_back and gap <= self.depart_confirm_sec and self._spell_start:
                self.person_since = self._spell_start   # 短暂没看到（遮挡、漏检）不算离开，接着算
            else:
                self.person_since = self.person_since or now
            self.person_absent_since = None
            self._depart_pending = False
            self._exit_inside = False
            # 进出事件: 空置超过 60 秒后有人出现 → 有人回来了 (无人值守模式交给入侵告警)
            if first_back and gap > self.depart_confirm_sec and self.mode != AWAY:
                self.notify('INFO', '有人回来了，当前在家 %d 人。' % max(count, 1),
                            event_type='arrival', cooldown=0, snapshot=snap('arrival'))
        else:
            if self.person_since is not None:          # 刚刚看不到人
                self._spell_start = self.person_since
                self._depart_pending = now - self.person_since > 120
                self._exit_inside = exit_via_entry is False
            self.person_absent_since = self.person_absent_since or now
            self.person_since = None
            # 进出事件: 在场超过 2 分钟、之后连续 60 秒都没看到人 → 通报离开（防漏检一帧就报“出门”）
            if self._depart_pending and not self._exit_inside \
                    and now - self.person_absent_since >= self.depart_confirm_sec:
                self._depart_pending = False
                left = max(self.away_after_sec - (now - self.person_absent_since), 0)
                self.notify('INFO', '人都出门了，约 %g 分钟后进入无人值守。' % round(left / 60, 1),
                            event_type='departure', snapshot=snap('departure'))
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
            # 最近一段时间里累计在场够久 → HOME
            if self.mode in (AWAY, PET) and sum(d for _, d in self._seen) >= self.home_confirm_sec:
                self.set_mode(HOME, '%g 分钟内累计看到人超过 %g 分钟'
                              % (self.home_window_sec / 60, self.home_confirm_sec / 60))
        elif now - self.person_absent_since > (self.away_inside_sec if self._exit_inside else self.away_after_sec):
            if only_pets and self.mode != PET:
                self.set_mode(PET, '家中只剩宠物')
            elif not only_pets and self.mode != AWAY:
                self.set_mode(AWAY, '最后看到的人没走大门，但已 %g 分钟没再出现' % (self.away_inside_sec / 60)
                              if self._exit_inside else '家中无人超过%g分钟' % (self.away_after_sec / 60))
        return self.mode

    def set_mode(self, mode, reason=''):
        if mode == self.mode:
            return self.mode
        old, self.mode = self.mode, mode
        if mode == AWAY:
            self._seen.clear()           # 进入无人值守后重新累计，之前在家的时间不算回家
        self.notify('INFO', '切换到%s（%s）。有事我叫%s。' % (MODE_CN.get(mode, mode), reason, self.owner),
                    event_type='mode_change', cooldown=0)
        return self.mode

    def set_manual_mode(self, mode, hours=4):
        self.manual_until = time.time() + hours * 3600
        self.set_mode(mode, '%s指令' % self.owner)
        self._save_state()

    # ---------- 指令状态落盘：重启后“是我”“模式”“静音”继续有效 ----------
    def _load_state(self):
        try:
            d = json.load(open(self.state_path, encoding='utf-8'))
        except (OSError, ValueError):
            return
        now = time.time()
        if float(d.get('manual_until', 0)) > now and d.get('mode') in MODE_CN:
            self.manual_until, self.mode = float(d['manual_until']), d['mode']
        if float(d.get('muted_until', 0)) > now:
            self.muted_until = float(d['muted_until'])

    def _save_state(self):
        try:
            tmp = self.state_path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({'mode': self.mode, 'manual_until': self.manual_until,
                           'muted_until': self.muted_until}, f)
            os.replace(tmp, self.state_path)
        except OSError as e:
            print('[butler] 指令状态保存失败：', e)

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
        # 事后补标 / 漏报：“没事 09:30”“属实 #7”“漏报 15:00 有人进来没报”
        from mibutler.brain import learning
        if learning.parse_label_command(t):
            return learning.apply_label_command(os.path.dirname(self.events_path), t, manager=self.incidents)
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
            self._save_state()
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
             curl -H "X-Butler-Token: $(cat .command_token)" -d "没事" http://127.0.0.1:8766/command
             python main.py cmd 没事
           只监听 127.0.0.1，不对局域网开放；必须带口令（.env 的 COMMAND_TOKEN，或自动生成的
           .command_token 文件），防止你打开的网页偷偷发“是我”“静音”关掉提醒。COMMAND_PORT=0 关闭。"""
        port = int(cfg('COMMAND_PORT', 8766) if port is None else port)
        if port <= 0:
            return None
        butler = self
        token = command_token(create=True)

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

            def _allowed(self):
                """挡住网页和 DNS 重绑定：浏览器发不出自定义请求头（会被 CORS 预检拦下），
                重绑定时 Host 不是 127.0.0.1；本机其他程序没有口令文件也发不了"""
                host = (self.headers.get('Host') or '').split(':')[0]
                if host not in ('127.0.0.1', 'localhost') or self.headers.get('Origin'):
                    return False
                return hmac.compare_digest(self.headers.get('X-Butler-Token') or '', token)

            def do_GET(self):
                self._reply(405, '请用 POST，并带上 X-Butler-Token 请求头（见 .command_token）。')

            def do_POST(self):
                if not self._allowed():
                    return self._reply(403, 'forbidden')
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


TOKEN_FILE = os.path.join(ROOT, '.command_token')


def command_token(create=False):
    """指令口令：优先 .env 的 COMMAND_TOKEN；否则读 / 生成仓库根目录的 .command_token（仅本人可读）"""
    t = (cfg('COMMAND_TOKEN', '') or '').strip()
    if t:
        return t
    try:
        with open(TOKEN_FILE, encoding='utf-8') as f:
            t = f.read().strip()
    except OSError:
        t = ''
    if not t and create:
        t = secrets.token_urlsafe(24)
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(t)
    return t


def send_command(text, port=None):
    """把一条指令发给正在运行的执事，返回执事的回答"""
    port = int(port or cfg('COMMAND_PORT', 8766))
    req = urllib.request.Request('http://127.0.0.1:%d/command' % port, data=text.encode('utf-8'),
                                 headers={'Content-Type': 'text/plain; charset=utf-8',
                                          'X-Butler-Token': command_token()})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode('utf-8')
