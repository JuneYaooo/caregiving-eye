# -*- coding: utf-8 -*-
"""守护：让执事一直在岗（python main.py serve）

照护系统最大的风险是“执事睡着了没人知道”。守护进程负责：
  - 按顺序拉起 go2rtc、xiaomi-onvif 桥（仓库根目录里有对应程序时）和执事本体；
  - 任何一个进程退出就自动重启，间隔 5、10、20 秒……最长 5 分钟；
  - 执事的心跳文件 events/status.json 超过 HEARTBEAT_SEC 秒（默认 180）没更新，
    说明卡死了，强制重启；
  - 执事意外退出、卡死重启都会通报；30 分钟内重启 5 次以上发急报（多半是摄像头、模型或配置坏了）；
  - 各进程的输出写到 events/logs/<名字>.log。

守护进程自己也可能随电脑一起停掉，这种情况它无法自报。外部机器人可以定时运行
`python main.py health`（或读取 status.json 的 time 字段），超过 5 分钟没更新就提醒“执事掉线”。
开机自启见 scripts/windows/install_autostart.ps1。
"""
import os, shutil, signal, subprocess, sys, time
from mibutler.config import cfg, ROOT


def _find_exe(name):
    for cand in (os.path.join(ROOT, name + '.exe'), os.path.join(ROOT, name)):
        if os.path.isfile(cand):
            return cand
    return shutil.which(name)


class Child:
    def __init__(self, name, cmd, log_dir, cwd=ROOT):
        self.name, self.cmd, self.cwd = name, cmd, cwd
        self.log_path = os.path.join(log_dir, '%s.log' % name)
        self.proc = None
        self.started = 0.0
        self.backoff = 5.0
        self.next_start = 0.0
        self.restarts = []            # 最近的重启时间

    def start(self):
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 20 * 1024 ** 2:
            os.replace(self.log_path, self.log_path + '.1')
        log = open(self.log_path, 'a', encoding='utf-8', errors='replace')
        log.write('\n===== %s 启动：%s =====\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(self.cmd)))
        log.flush()
        env = dict(os.environ, PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8')
        self.proc = subprocess.Popen(self.cmd, cwd=self.cwd, stdout=log, stderr=subprocess.STDOUT, env=env)
        self.started = time.time()
        print('[serve] 启动 %s（pid %d），输出见 %s' % (self.name, self.proc.pid, self.log_path))

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self, timeout=15):
        if not self.alive():
            return
        try:
            if os.name == 'nt':
                self.proc.terminate()                     # Windows 没有 SIGINT；当天统计每 5 分钟已落盘
            else:
                self.proc.send_signal(signal.SIGINT)      # 让执事保存当天统计再退出
            self.proc.wait(timeout)
        except Exception:
            self.proc.kill()


class Supervisor:
    def __init__(self, run_args, events_dir=None, stack=True, heartbeat_sec=None):
        self.ev_dir = events_dir or cfg('EVENTS_DIR', 'events')
        self.status_path = os.path.join(self.ev_dir, 'status.json')
        self.heartbeat_sec = float(heartbeat_sec or cfg('HEARTBEAT_SEC', 180))
        log_dir = os.path.join(self.ev_dir, 'logs')
        self.children = []
        if stack:
            go2rtc, onvif = _find_exe('go2rtc'), _find_exe('xiaomi-onvif')
            yaml = os.path.join(ROOT, 'go2rtc.yaml')
            if go2rtc and os.path.exists(yaml):
                self.children.append(Child('go2rtc', [go2rtc, '-config', yaml], log_dir))
            if onvif:
                self.children.append(Child('onvif', [
                    onvif, '-go2rtc=' + cfg('GO2RTC_API', 'http://127.0.0.1:1984'),
                    '-rtsp=' + cfg('RTSP_URL', 'rtsp://127.0.0.1:8554/living').rsplit('/', 1)[0],
                    '-camera=living=127.0.0.1:8891,living'], log_dir))
        self.butler = Child('butler', [sys.executable, os.path.join(ROOT, 'main.py'), 'run'] + list(run_args),
                            log_dir)
        self.children.append(self.butler)
        self._notifier = None

    def notify(self, level, text):
        """借执事的通报通道（alerts.jsonl + webhook）发消息"""
        try:
            if self._notifier is None:
                from mibutler.butler import Butler
                self._notifier = Butler(events_path=os.path.join(self.ev_dir, 'alerts.jsonl'))
            self._notifier.notify(level, text, event_type='supervisor', cooldown=0)
            time.sleep(0.5)       # 给推送线程一点时间
        except Exception as e:
            print('[serve] 通报失败：', e)

    def _heartbeat_age(self):
        try:
            return time.time() - os.path.getmtime(self.status_path)
        except OSError:
            return None

    def _restart(self, c, why):
        now = time.time()
        c.restarts = [t for t in c.restarts if now - t < 1800] + [now]
        c.stop()
        c.next_start = now + c.backoff
        c.backoff = min(c.backoff * 2, 300.0)
        print('[serve] %s：%s，%d 秒后重启' % (c.name, why, c.next_start - now))
        if c is self.butler:
            if len(c.restarts) >= 5:
                self.notify('CRITICAL', '执事 30 分钟内已重启 %d 次（%s），可能是摄像头、模型或配置出了问题，'
                                        '请查看 %s。' % (len(c.restarts), why, c.log_path))
            else:
                self.notify('GUARD', '执事%s，已自动重启。' % why)
        elif len(c.restarts) >= 5:
            self.notify('GUARD', '%s 30 分钟内已重启 %d 次，画面可能中断，请查看 %s。'
                        % (c.name, len(c.restarts), c.log_path))

    def tick(self):
        now = time.time()
        for c in self.children:
            if c.proc is None:
                if now >= c.next_start:
                    c.start()
                    if c.name == 'go2rtc':
                        time.sleep(4)          # 等 go2rtc 起来再启动桥和执事
                continue
            code = c.proc.poll()
            if code is not None:
                c.proc = None
                self._restart(c, '意外退出（退出码 %s）' % code)
                continue
            if now - c.started > 600:
                c.backoff = 5.0                # 稳定运行 10 分钟后重置退避
            if c is self.butler and now - c.started > self.heartbeat_sec:
                age = self._heartbeat_age()
                if age is not None and age > self.heartbeat_sec:
                    self._restart(c, ' %d 秒没有心跳，疑似卡死' % age)
                    c.proc = None

    def run(self):
        print('[serve] 守护启动：%s' % ', '.join(c.name for c in self.children))
        try:
            while True:
                self.tick()
                time.sleep(5)
        except KeyboardInterrupt:
            print('[serve] 收到退出指令，正在停止…')
        finally:
            for c in reversed(self.children):
                c.stop()


def health(events_dir=None, max_age=300):
    """给外部机器人用：执事心跳正常返回 0，掉线返回 1"""
    import json
    path = os.path.join(events_dir or cfg('EVENTS_DIR', 'events'), 'status.json')
    try:
        age = time.time() - os.path.getmtime(path)
        st = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError):
        print('执事掉线：找不到心跳文件 %s' % path)
        return 1
    if age > max_age:
        print('执事掉线：%d 分钟没有心跳（最后一次 %s）' % (age / 60, st.get('time')))
        return 1
    print('执事在岗：%s，%s，画面%s，未结事件 %d 件' % (
        st.get('mode_cn'), st.get('level'),
        '正常' if (st.get('frames') or {}).get('connected') else '中断',
        len(st.get('open_incidents') or [])))
    return 0
