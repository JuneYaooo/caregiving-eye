# -*- coding: utf-8 -*-
"""每周复盘：统计最近几天哪里老误报、哪些没人确认、漏报了什么，给出调整建议（只建议，不改任何东西）。

用法: python weekly_review.py [--events-dir events] [--days 7] [--json]

读取 alerts.jsonl / incidents.jsonl / labels.jsonl / household.json。
建议的落地方式（都要主人同意后再执行）：
  household.py add-object ...    把老误报的位置标成静物区
  household.py add-lighting ...  记下光照规律
  main.py cmd 没事 HH:MM          给没人确认的提醒补标
"""
import argparse, glob, json, os, sys, time
from collections import Counter, defaultdict

SERIOUS = ('CRITICAL', 'GUARD')
PERSON_TYPES = {'fall', 'fall_suspect', 'lying_long', 'illness', 'inactive', 'zone_dwell', 'intrusion',
                'night_wander', 'child_danger', 'child_alone'}
TYPE_CN = {'fall': '跌倒', 'fall_suspect': '疑似跌倒', 'lying_long': '倒地久卧', 'illness': '突发不适',
           'intrusion': '入侵', 'night_wander': '夜间离床', 'zone_dwell': '区域久滞', 'fire': '火情',
           'fire_suspect': '疑似烟火', 'cat_zone': '宠物进禁区', 'glass_break': '玻璃碎裂声'}


def read_jsonl(pattern):
    out = []
    for p in sorted(glob.glob(pattern)):
        for line in open(p, encoding='utf-8'):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def cell_roi(cell):
    """误报学习的 6×4 格子 'c3_2' → 相对坐标框"""
    try:
        c, r = [int(x) for x in cell[1:].split('_')]
    except Exception:
        return None
    return [round(c / 6, 3), round(r / 4, 3), round((c + 1) / 6, 3), round((r + 1) / 4, 3)]


def review(events_dir, days=7, now=None):
    now = now or time.time()
    since = time.strftime('%Y-%m-%d', time.localtime(now - days * 86400))
    inc = [i for i in read_jsonl(os.path.join(events_dir, 'incidents*.jsonl')) if i.get('opened', '') >= since]
    alerts = [a for a in read_jsonl(os.path.join(events_dir, 'alerts*.jsonl')) if a.get('ts', '') >= since]
    labels = [l for l in read_jsonl(os.path.join(events_dir, 'labels.jsonl')) if l.get('ts', 0) >= now - days * 86400]
    hh_path = os.path.join(events_dir, 'household.json')
    household = json.load(open(hh_path, encoding='utf-8')) if os.path.exists(hh_path) else {}

    # 每件事的最终结论：事后补标优先于当场回复
    final = {i.get('id'): i.get('acked') for i in inc}
    for l in sorted(labels, key=lambda x: x['ts']):
        if l.get('incident') and l['kind'] in ('false', 'true'):
            final[l['incident']] = l['kind']

    by_spot = defaultdict(lambda: {'n': 0, 'false': 0, 'true': 0, 'open': 0, 'hours': Counter(), 'days': set(), 'times': []})
    for i in inc:
        if i.get('level') not in SERIOUS:
            continue
        verdict = final.get(i.get('id'))
        for t in set(i.get('types') or []):
            for cell in (i.get('cells') or [None]):
                s = by_spot[(t, cell)]
                s['n'] += 1
                s['false'] += verdict == 'false'
                s['true'] += verdict == 'true'
                s['open'] += verdict not in ('false', 'true', 'me')
                s['hours'][int(i['opened'][11:13])] += 1
                s['days'].add(i['opened'][:10])
                if verdict not in ('false', 'true', 'me'):
                    s['times'].append(i['opened'][5:16])

    suggestions = []
    for (t, cell), s in sorted(by_spot.items(), key=lambda kv: -kv[1]['n']):
        name = TYPE_CN.get(t, t)
        if t in PERSON_TYPES and cell and s['false'] >= 2 and s['true'] == 0:
            roi = cell_roi(cell)
            suggestions.append({
                'kind': 'static_object', 'type': t, 'cell': cell,
                'why': '「%s」在画面位置 %s 被确认误报 %d 次、从未属实' % (name, cell, s['false']),
                'action': '看一眼该位置的快照：如果是雕像、挂衣、立牌等静物，标成静物区（这里没走过路的人形不再算人）',
                'command': 'household.py add-object --name <静物名> --roi %s' % ','.join(map(str, roi))})
        top_hour, top_n = (s['hours'].most_common(1) or [(None, 0)])[0]
        if s['false'] >= 2 and len(s['days']) >= 2 and top_n >= 0.6 * s['n']:
            suggestions.append({
                'kind': 'lighting', 'type': t, 'cell': cell,
                'why': '「%s」的误报 %d 次里有 %d 次集中在 %02d 点前后，跨 %d 天' % (name, s['false'], top_n, top_hour, len(s['days'])),
                'action': '可能是每天固定时段的光照或阴影，确认后记一条光照规律',
                'command': "household.py add-lighting --time %02d:00-%02d:00 --note '<现象>'" % (top_hour, (top_hour + 1) % 24)})
        if s['open'] >= 3:
            suggestions.append({
                'kind': 'unlabeled', 'type': t, 'cell': cell,
                'why': '「%s」有 %d 条值守/急报一直没人确认' % (name, s['open']),
                'action': '看快照判断真假后补标，误报学习和跌倒模型才会进步',
                'command': '  '.join('main.py cmd 没事 %s' % x[6:] for x in s['times'][-3:]) + '  （真事件用“属实”）'})

    missed = [l for l in labels if l['kind'] == 'missed']
    down = [a for a in alerts if a.get('type') == 'stream_down']
    for m in missed:
        day = m.get('day', '')
        hm = (m.get('when') or '')[:5]
        near_down = [a['ts'][5:16] for a in down if a['ts'].startswith(day) and abs(
            int(a['ts'][11:13]) * 60 + int(a['ts'][14:16]) - (int(hm[:2]) * 60 + int(hm[3:5]) if ':' in hm else -999)) < 60]
        cam = household.get('camera') or {}
        if near_down:
            why = '那段时间画面中断过（%s），执事看不到' % '、'.join(near_down)
            act = '检查网络或摄像头供电；画面中断时的通报要能推到手机上'
        elif cam.get('covers_door') is False:
            why = '大门不在画面里，人可能没走进镜头'
            act = '考虑调整摄像头朝向，或在门口加一个摄像头'
        else:
            why = '原因待查：当时画面在线'
            act = '用 home-review 回看当时的快照或米家回放：人是否进了画面、是否太远太小、当时是什么模式'
        suggestions.append({'kind': 'missed', 'why': '漏报 %s %s：%s' % (m.get('day', '')[5:], m.get('when'), m.get('note', '')) + '。' + why,
                            'action': act, 'command': None})

    labeled = sum(1 for v in final.values() if v in ('false', 'true'))
    samples = glob.glob(os.path.join(events_dir, 'samples', 'fall_*.json'))
    lab_samples = 0
    for p in samples:
        try:
            lab_samples += json.load(open(p, encoding='utf-8')).get('label') is not None
        except Exception:
            pass
    if lab_samples >= 20:
        suggestions.append({'kind': 'retrain', 'why': '已有 %d 个标好的跌倒样本' % lab_samples,
                            'action': '可以用家里的样本重新训练骨架跌倒模型，减少这个家特有的误报',
                            'command': 'python scripts/fall_gcn/train.py --samples events/samples'})

    return {
        'window': '%s 起 %d 天' % (since, days),
        'totals': {'serious': sum(1 for i in inc if i.get('level') in SERIOUS),
                   'labeled': labeled, 'missed': len(missed),
                   'stream_down': len(down), 'fall_samples': len(samples), 'fall_samples_labeled': lab_samples},
        'spots': [{'type': t, 'cell': c, 'n': s['n'], 'false': s['false'], 'true': s['true'], 'unlabeled': s['open']}
                  for (t, c), s in sorted(by_spot.items(), key=lambda kv: -kv[1]['n'])][:10],
        'suggestions': suggestions,
        'household_known': {'static_objects': [o.get('name') for o in household.get('static_objects', [])],
                            'facts': len(household.get('facts', []))},
    }


def to_text(r):
    t = r['totals']
    L = ['# 每周复盘（%s）' % r['window'],
         '值守/急报 %d 件，已标真假 %d 件；漏报记录 %d 条；画面中断 %d 次；跌倒样本 %d 个（已标 %d）'
         % (t['serious'], t['labeled'], t['missed'], t['stream_down'], t['fall_samples'], t['fall_samples_labeled'])]
    if r['spots']:
        L += ['', '## 提醒最多的位置', '| 类型 | 位置 | 次数 | 误报 | 属实 | 未确认 |', '|---|---|---|---|---|---|']
        for s in r['spots']:
            L.append('| %s | %s | %d | %d | %d | %d |' % (TYPE_CN.get(s['type'], s['type']), s['cell'] or '-',
                                                         s['n'], s['false'], s['true'], s['unlabeled']))
    L += ['', '## 建议（需主人同意后再执行）']
    if not r['suggestions']:
        L.append('暂无。标记的真假越多，这里的建议越准。')
    for i, s in enumerate(r['suggestions'], 1):
        L.append('%d. %s\n   → %s' % (i, s['why'], s['action']))
        if s.get('command'):
            L.append('   `%s`' % s['command'])
    return '\n'.join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--events-dir', default=os.environ.get('EVENTS_DIR', 'events'))
    ap.add_argument('--days', type=int, default=7)
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if not os.path.isdir(a.events_dir):
        sys.exit('找不到事件目录 %s' % a.events_dir)
    r = review(a.events_dir, a.days)
    print(json.dumps(r, ensure_ascii=False, indent=1) if a.json else to_text(r))


if __name__ == '__main__':
    main()
