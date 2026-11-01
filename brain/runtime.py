# -*- coding: utf-8 -*-
"""任务调度：执事的“值班表”

三类任务，三条车道：
  常驻服务 service   独立线程长期运行：拉流、听声音。由看门狗监控，挂了自动重启。
  周期任务 periodic  按间隔反复执行。间隔可以随“警觉等级”变化（闲时慢、警觉时快），
                     也可以由“新检测结果”触发（trigger='detect'）。
  一次性任务 once    延时或立即执行一次：写录像、发通知、转云台、生成日报。
  每日任务 daily     每天固定时刻执行一次。

车道：
  cpu  在主循环里串行执行。模型推理都在这里，串行可以避免多个模型抢 CPU。
  io   放进小线程池并行执行：写文件、发网络请求、转云台，不阻塞主循环。

保护：
  - 优先级：数字越小越重要。一轮耗时超过预算时，优先级 >= SHED_PRIORITY 的任务本轮跳过。
  - 失败退避：任务抛异常不会拖垮主循环；连续失败按 10s、20s、40s…最多 10 分钟暂停后重试。
"""
import concurrent.futures as cf
import threading, time, traceback

SHED_PRIORITY = 5


class Task:
    def __init__(self, name, fn, every=1.0, priority=5, lane='cpu', trigger=None,
                 modes=None, when=None, desc=''):
        self.name, self.fn = name, fn
        self.every = every            # 秒 | {level: 秒} | callable(ctx)->秒 | None=暂停
        self.priority, self.lane = priority, lane
        self.trigger = trigger        # 'detect'：每次有新检测结果时执行
        self.modes = set(modes) if modes else None
        self.when = when
        self.desc = desc
        self.next_due = 0.0
        self.last_seq = -1
        self.cost = 0.0               # 平均耗时 (秒, EMA)
        self.runs = self.skips = self.fails = 0
        self.disabled_until = 0.0
        self.running = False
        self.last_error = ''

    def interval(self, ctx):
        e = self.every
        if callable(e):
            return e(ctx)
        if isinstance(e, dict):
            return e.get(ctx.level, e.get('default'))
        return e


class Scheduler:
    def __init__(self, io_workers=2, budget=0.6):
        self.tasks = []
        self.pool = cf.ThreadPoolExecutor(max_workers=io_workers, thread_name_prefix='io')
        self.budget = budget          # 一轮 cpu 任务的耗时预算 (秒)
        self._once = []               # [(due, name, fn, lane)]
        self._daily = []              # [{'name','hhmm','fn','lane','last'}]
        self._lock = threading.Lock()

    # ---------- 注册 ----------
    def add(self, task):
        self.tasks.append(task)
        self.tasks.sort(key=lambda t: t.priority)
        return task

    def once(self, name, fn, delay=0.0, lane='io'):
        with self._lock:
            self._once.append((time.time() + delay, name, fn, lane))

    def daily(self, name, hhmm, fn, lane='io'):
        self._daily.append({'name': name, 'hhmm': hhmm, 'fn': fn, 'lane': lane,
                            'last': time.strftime('%Y%m%d') if time.strftime('%H:%M') >= hhmm else ''})

    # ---------- 执行 ----------
    def _eligible(self, t, ctx, now):
        if now < t.disabled_until or t.running:
            return False
        if t.modes and ctx.mode not in t.modes:
            return False
        if t.trigger == 'detect':
            if ctx.det_seq == t.last_seq:
                return False
        elif now < t.next_due:
            return False
        if t.when and not t.when(ctx):
            if t.trigger == 'detect':
                t.last_seq = ctx.det_seq
            else:
                t.next_due = now + 1.0
            return False
        return True

    def _run(self, t, ctx):
        t0 = time.time()
        try:
            t.fn(ctx)
            t.fails = 0
        except Exception as e:
            t.fails += 1
            t.last_error = '%s: %s' % (type(e).__name__, e)
            backoff = min(10 * 2 ** (t.fails - 1), 600)
            t.disabled_until = time.time() + backoff
            print('[task] %s 失败（第 %d 次），%ds 后重试：%s' % (t.name, t.fails, backoff, t.last_error))
            if t.fails == 1:
                traceback.print_exc()
        finally:
            dt = time.time() - t0
            t.cost = dt if t.runs == 0 else t.cost * 0.9 + dt * 0.1
            t.runs += 1
            t.running = False

    def tick(self, ctx):
        now = time.time()
        start = now
        for t in self.tasks:
            if not self._eligible(t, ctx, now):
                continue
            if t.trigger == 'detect':
                t.last_seq = ctx.det_seq
            else:
                iv = t.interval(ctx)
                if iv is None:
                    t.next_due = now + 1.0
                    continue
                t.next_due = now + iv
            if t.lane == 'io':
                t.running = True
                self.pool.submit(self._run, t, ctx)
                continue
            if t.priority >= SHED_PRIORITY and time.time() - start > self.budget:
                t.skips += 1          # 过载：低优先级本轮让路
                continue
            self._run(t, ctx)
        # 一次性任务
        with self._lock:
            due = [o for o in self._once if o[0] <= now]
            self._once = [o for o in self._once if o[0] > now]
        for _, name, fn, lane in due:
            t = Task(name, lambda c, f=fn: f(), lane=lane)
            if lane == 'io':
                self.pool.submit(self._run, t, ctx)
            else:
                self._run(t, ctx)
        # 每日任务
        today, hm = time.strftime('%Y%m%d'), time.strftime('%H:%M')
        for d in self._daily:
            if hm >= d['hhmm'] and d['last'] != today:
                d['last'] = today
                self.once(d['name'], d['fn'], lane=d['lane'])

    def next_wakeup(self, ctx):
        """距离下一个周期任务到期还有多久，主循环据此休眠"""
        now = time.time()
        dues = [t.next_due for t in self.tasks if t.trigger is None and now >= t.disabled_until]
        return max(0.02, min(dues, default=now + 0.2) - now)

    def stats(self):
        return [{'name': t.name, 'priority': t.priority, 'lane': t.lane, 'runs': t.runs,
                 'avg_ms': round(t.cost * 1000, 1), 'skips': t.skips, 'fails': t.fails,
                 'paused': t.disabled_until > time.time(), 'error': t.last_error}
                for t in self.tasks]

    def shutdown(self):
        self.pool.shutdown(wait=True)
