# -*- coding: utf-8 -*-
"""作息偏离：拿今天的活动和过去几周同一时段比

数据来自 ActivityLog('person') 每天写的 events/activity_person_YYYYMMDD.json：
  hourly_active  每小时走动的秒数      hourly_seen  每小时在画面里的秒数
  morning_first  当天 ROUTINE_DAY_START 点以后第一次走动的时间（HH:MM）
攒够 ROUTINE_MIN_DAYS 天（默认 7）才开始对比，之前只在日报里显示“还在攒数据”。

三条规则，每条每天最多提醒一次：
  起床晚   平时 07:20 前后开始走动，今天过了“平时时间 + ROUTINE_LATE_MIN 分钟”（且不早于
           历史上最晚那几天）还没看到人走动 → 值守提醒。前一晚 16 小时内见过人才判断，
           人本来就不在家（出差、住院）不提醒。
  活动少   12 点以后，今天到目前的走动时间不到平时同时段的 ROUTINE_LOW_RATIO（默认 35%），
           但在画面里的时间并不少（人在家，只是不怎么动）→ 值守提醒。
           人在画面里的时间也少，多半是出门了，不提醒。
  夜里多动 早上回顾：凌晨 0 点到 ROUTINE_DAY_START 的走动时间超过平时的 3 倍且多于 15 分钟
           → 普通提醒（失眠、起夜频繁、身体不适都可能）。

阈值用中位数和分位数，偶尔一两天的特殊情况不会把基线带偏。
"""
import glob, json, os, time
import numpy as np


def _hm(minutes):
    minutes = int(round(minutes)) % (24 * 60)
    return '%02d:%02d' % (minutes // 60, minutes % 60)


def _to_min(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


class RoutineMonitor:
    def __init__(self, events_dir, kind='person', day_start=4, history_days=28, min_days=7,
                 late_min=90, low_ratio=0.35, night_ratio=3.0):
        self.dir, self.kind = events_dir, kind
        self.day_start = day_start
        self.history_days, self.min_days = history_days, min_days
        self.late_min, self.low_ratio, self.night_ratio = late_min, low_ratio, night_ratio
        self._hist_day, self._hist = None, []
        self.fired = {}          # 规则 -> 已提醒的日期

    @classmethod
    def from_env(cls, events_dir, cfg):
        if cfg('ROUTINE_ENABLE', '1').strip() in ('0', 'false', 'no'):
            return None
        return cls(events_dir, day_start=int(cfg('ROUTINE_DAY_START', 4)),
                   history_days=int(cfg('ROUTINE_HISTORY_DAYS', 28)),
                   min_days=int(cfg('ROUTINE_MIN_DAYS', 7)),
                   late_min=float(cfg('ROUTINE_LATE_MIN', 90)),
                   low_ratio=float(cfg('ROUTINE_LOW_RATIO', 0.35)))

    # ---------- 历史 ----------
    def history(self, today):
        """过去 history_days 天（不含今天）有记录的日子，按日期升序"""
        if self._hist_day == today:
            return self._hist
        days = []
        for p in sorted(glob.glob(os.path.join(self.dir, 'activity_%s_*.json' % self.kind))):
            d = os.path.basename(p)[len('activity_%s_' % self.kind):-5]
            if not d.isdigit() or d >= today:
                continue
            try:
                s = json.load(open(p, encoding='utf-8'))
            except Exception:
                continue
            if sum(s.get('hourly_seen') or [0]) < 600:   # 全天在画面里不到 10 分钟：多半不在家，不当基线
                continue
            days.append(s)
        self._hist_day, self._hist = today, days[-self.history_days:]
        return self._hist

    def _wake_min(self, s):
        """当天开始走动的时间（分钟）；老数据没有 morning_first 时用 hourly_active 估到整点"""
        if s.get('morning_first'):
            return _to_min(s['morning_first'])
        ha = s.get('hourly_active') or [0] * 24
        for h in range(self.day_start, 24):
            if ha[h] >= 30:
                return h * 60
        return None

    def _cum(self, arr, upto_hour):
        return float(sum((arr or [0] * 24)[self.day_start:upto_hour]))

    def baseline(self, today):
        hist = self.history(today)
        if len(hist) < self.min_days:
            return None
        wakes = [w for w in (self._wake_min(s) for s in hist) if w is not None]
        return {
            'days': len(hist),
            'wake_med': float(np.median(wakes)) if len(wakes) >= self.min_days else None,
            'wake_p90': float(np.percentile(wakes, 90)) if len(wakes) >= self.min_days else None,
            'active': np.median([s.get('hourly_active') or [0] * 24 for s in hist], axis=0),
            'seen': np.median([s.get('hourly_seen') or [0] * 24 for s in hist], axis=0),
            'night': float(np.median([sum((s.get('hourly_active') or [0] * 24)[:self.day_start])
                                      for s in hist])),
        }

    # ---------- 实时检查（每 10 分钟） ----------
    def check(self, today_stats, now, last_person_ts):
        """返回 [(level, event_type, text)]"""
        lt = time.localtime(now)
        day = time.strftime('%Y%m%d', lt)
        b = self.baseline(day)
        if b is None:
            return []
        out = []
        cur = lt.tm_hour * 60 + lt.tm_min
        s = today_stats or {}

        # 起床晚
        if b['wake_med'] is not None and self.fired.get('late') != day and not s.get('morning_first') \
                and cur >= self.day_start * 60:
            late_at = max(b['wake_med'] + self.late_min, b['wake_p90'] + 30)
            last = last_person_ts or self._last_seen_before(day)
            home_last_night = bool(last) and now - last < 16 * 3600
            if cur >= late_at and home_last_night:
                self.fired['late'] = day
                out.append(('GUARD', 'routine_late',
                            '平时 %s 前后开始有走动，今天到 %s 还没看到人起来活动。如果家人已经出门，可以忽略。'
                            % (_hm(b['wake_med']), _hm(cur))))

        # 活动少（12 点以后，按已过去的整点算）
        h = lt.tm_hour
        if h >= 12 and self.fired.get('low') != day:
            usual_act = float(b['active'][self.day_start:h].sum())
            usual_seen = float(b['seen'][self.day_start:h].sum())
            act = self._cum(s.get('hourly_active'), h)
            seen = self._cum(s.get('hourly_seen'), h)
            if usual_act >= 1200 and seen >= 0.5 * usual_seen and act < self.low_ratio * usual_act:
                self.fired['low'] = day
                out.append(('GUARD', 'routine_low',
                            '今天到 %02d:00 走动约 %d 分钟，只有平时同一时段的 %d%%（平时约 %d 分钟），'
                            '人一直在家但活动明显偏少，请留意身体状况。'
                            % (h, act / 60, act / usual_act * 100, usual_act / 60)))

        # 夜里多动（早上回顾一次）
        if h >= self.day_start and self.fired.get('night') != day:
            self.fired['night'] = day
            night = self._night(s)
            if night > 900 and night > self.night_ratio * max(b['night'], 60):
                out.append(('INFO', 'routine_night',
                            '昨夜 0–%d 点走动约 %d 分钟，平时约 %d 分钟，明显偏多（失眠、起夜频繁或身体不适都可能）。'
                            % (self.day_start, night / 60, b['night'] / 60)))
        return out

    def _last_seen_before(self, day):
        """执事刚重启、今天还没见过人时，从昨天的记录里取最后一次见到人的时间"""
        y = time.strftime('%Y%m%d', time.localtime(time.mktime(time.strptime(day, '%Y%m%d')) - 43200))
        p = os.path.join(self.dir, 'activity_%s_%s.json' % (self.kind, y))
        try:
            return json.load(open(p, encoding='utf-8')).get('last_seen_ts')
        except Exception:
            return None

    def _night(self, s):
        return float(sum((s.get('hourly_active') or [0] * 24)[:self.day_start]))

    # ---------- 日报 ----------
    def summary(self, today_stats, day):
        hist = self.history(day)
        if len(hist) < self.min_days:
            return ['作息对比：已积累 %d 天数据，满 %d 天后开始和平时对比。' % (len(hist), self.min_days)]
        b = self.baseline(day)
        s = today_stats or {}
        parts = []
        w = self._wake_min(s) if s else None
        if w is not None and b['wake_med'] is not None:
            diff = w - b['wake_med']
            parts.append('开始走动 %s（平时 %s%s）' % (
                _hm(w), _hm(b['wake_med']),
                '' if abs(diff) < 30 else '，%s %d 分钟' % ('晚' if diff > 0 else '早', abs(diff))))
        act = sum(s.get('hourly_active') or [0])
        usual = float(b['active'].sum())
        if usual > 0:
            parts.append('全天走动 %d 分钟，为平时的 %d%%' % (act / 60, act / usual * 100))
        night = self._night(s)
        if night > 300:
            parts.append('凌晨走动 %d 分钟（平时 %d 分钟）' % (night / 60, b['night'] / 60))
        return ['作息对比（近 %d 天）：%s。' % (b['days'], '；'.join(parts) if parts else '今天数据不足')]
