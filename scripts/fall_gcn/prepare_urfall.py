# -*- coding: utf-8 -*-
"""第 0 步：下载 UR Fall Detection Dataset（cam0 视角）并生成标签表

数据集：University of Rzeszow，30 段跌倒 + 40 段日常活动（ADL：走动、坐下、弯腰捡东西、
躺上床等，正好是最容易误报的动作）。主页 https://fenix.ur.edu.pl/mkepski/ds/uf.html
数据集采用 CC BY-NC-SA 4.0（非商业、相同方式共享），使用时请引用：
  Kwolek B., Kepski M. Human fall detection on embedded platform using depth maps and wireless
  accelerometer. Computer Methods and Programs in Biomedicine, 117(3), 2014.
本项目不分发数据集，也不分发用它训练出的权重，请在本机自行下载和训练。

用法:
  python scripts/fall_gcn/prepare_urfall.py --out data/urfall
输出:
  data/urfall/*.mp4                  视频（约 60 MB）
  data/urfall/labels.csv             name,fall_start,fall_end（秒；日常活动行留空）

官方逐帧标注：-1 未倒地、0 正在倒下、1 已躺在地上。这里取第一帧 0 为 fall_start、
第一帧 1 为 fall_end。视频帧率 30 fps，标注帧号从 1 开始。
"""
import argparse, collections, csv, os, urllib.request

BASE = 'https://fenix.ur.edu.pl/~mkepski/ds/data/'
FPS = 30.0


def fetch(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    print('下载', url)
    urllib.request.urlretrieve(url, path + '.part')
    os.replace(path + '.part', path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default='data/urfall')
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    falls_csv = os.path.join(args.out, 'urfall-cam0-falls.csv')
    fetch(BASE + 'urfall-cam0-falls.csv', falls_csv)
    for i in range(1, 31):
        fetch(BASE + 'fall-%02d-cam0.mp4' % i, os.path.join(args.out, 'fall-%02d-cam0.mp4' % i))
    for i in range(1, 41):
        fetch(BASE + 'adl-%02d-cam0.mp4' % i, os.path.join(args.out, 'adl-%02d-cam0.mp4' % i))

    seq = collections.defaultdict(list)
    for r in csv.reader(open(falls_csv)):
        seq[r[0]].append((int(r[1]), int(r[2])))
    with open(os.path.join(args.out, 'labels.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['name', 'fall_start', 'fall_end'])
        for name, rows in sorted(seq.items()):
            f0 = min(fr for fr, lab in rows if lab == 0)
            f1 = min(fr for fr, lab in rows if lab == 1)
            w.writerow(['%s-cam0' % name, '%.2f' % ((f0 - 1) / FPS), '%.2f' % ((f1 - 1) / FPS)])
        for i in range(1, 41):
            w.writerow(['adl-%02d-cam0' % i, '', ''])
    print('完成：%s' % os.path.join(args.out, 'labels.csv'))


if __name__ == '__main__':
    main()
