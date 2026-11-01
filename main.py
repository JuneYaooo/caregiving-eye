# -*- coding: utf-8 -*-
"""米执事命令行入口

  接入摄像头
    python main.py login [--force]          米家登录，生成 mi_creds.json
    python main.py devices                  查询米家设备，写入 mi_devices.json
    python main.py find-camera              扫描局域网里的小米设备
    python main.py ptz right 1.0            手动转云台（left/right/up/down + 秒数；stop；test）
    python main.py check --source rtsp://…  检查视频流和模型：读几十帧，打印帧率和检测结果

  值班
    python main.py run --source rtsp://…    启动执事（前台运行，Ctrl+C 退出）
    python main.py serve --source rtsp://…  守护模式：连同 go2rtc、云台桥一起拉起，崩溃或卡死自动重启
    python main.py health                   执事是否在岗（给外部机器人用，掉线时退出码为 1）

  日常
    python main.py cmd 没事                 把一条指令发给正在运行的执事
    python main.py cmd 没事 10:29           事后补标：那条是误报（属实 / 漏报 15:00 描述 同理）
    python main.py report [YYYYMMDD]        生成日报
"""
import argparse, os, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)


def cmd_run(args):
    from mibutler.app import App
    App(args).run()


def cmd_serve(args, rest):
    from mibutler.brain.supervisor import Supervisor
    Supervisor(rest + ['--events-dir', args.events_dir], events_dir=args.events_dir,
               stack=not args.no_stack).run()


def cmd_health(args):
    from mibutler.brain.supervisor import health
    sys.exit(health(args.events_dir, max_age=args.max_age))


def cmd_check(args):
    """读一段视频流（或一张图片）跑检测和姿态，确认视频、模型都正常"""
    import cv2
    from mibutler.vision.yolo import YoloOnnx, CLASS_NAMES
    det = YoloOnnx(args.det)
    pose = YoloOnnx(args.pose) if os.path.exists(args.pose) else None
    print('[check] 检测模型 %s；姿态模型 %s' % (args.det, args.pose if pose else '缺失（跌倒等会退回检测框规则）'))
    if args.image:
        frames = [cv2.imread(args.image)]
    else:
        cap = cv2.VideoCapture(args.source)
        if not cap.isOpened():
            sys.exit('[check] 打不开视频源：%s' % args.source)
        frames, t0 = [], time.time()
        while len(frames) < args.frames:
            ok, f = cap.read()
            if not ok:
                break
            frames.append(f)
        cap.release()
        if not frames:
            sys.exit('[check] 没读到画面：%s' % args.source)
        h, w = frames[0].shape[:2]
        print('[check] 读到 %d 帧，%dx%d，约 %.1f fps' % (len(frames), w, h, len(frames) / max(time.time() - t0, 1e-3)))
    t0 = time.time()
    seen = {}
    for f in frames[::max(1, len(frames) // 10)]:
        for d in det(f):
            name = CLASS_NAMES.get(d['cls'])
            if name:
                seen[name] = max(seen.get(name, 0), d['score'])
        if pose:
            pose(f, 0.3)
    n = len(frames[::max(1, len(frames) // 10)])
    print('[check] 推理 %d 帧，平均 %.0f ms/帧（检测%s）' % (n, (time.time() - t0) / n * 1000, '+姿态' if pose else ''))
    print('[check] 检出：%s' % ('，'.join('%s %.2f' % kv for kv in seen.items()) or '画面里没有人或猫狗'))


def cmd_devices(args):
    import json
    from mibutler.camera.mi_api import MiApi, load_creds
    devices = MiApi(load_creds()).device_list()
    path = os.path.join(ROOT, 'mi_devices.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(devices, f, ensure_ascii=False, indent=2)
    for d in devices:
        print('-', d.get('name'), '|', d.get('model'), '|', d.get('did'), '|', d.get('localip'))
    print('已写入 %s（含敏感信息，勿提交）' % path)


def cmd_report(args):
    from mibutler.report import daily_report
    daily_report(args.day, events_dir=args.events_dir)


def cmd_send(args):
    from mibutler.butler import send_command
    from mibutler.brain import learning
    from mibutler.config import cfg
    text = ' '.join(args.text)
    try:
        print(send_command(text))
    except OSError:
        # 执事没在跑：补标 / 漏报直接写文件，下次启动生效；其他指令需要执事在线
        if learning.parse_label_command(text):
            print(learning.apply_label_command(cfg('EVENTS_DIR', 'events'), text) + '（执事未运行，已写入记录，下次启动生效）')
        else:
            print('执事没有在运行（指令入口连不上）。')


def main():
    from mibutler.config import cfg
    from mibutler.app import add_run_args
    models = cfg('MODELS_DIR', 'models')
    ev_default = cfg('EVENTS_DIR', 'events')

    ap = argparse.ArgumentParser(description='米执事 · Mi Butler', formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest='cmd', metavar='命令')

    add_run_args(sub.add_parser('run', help='启动执事'))
    p = sub.add_parser('serve', help='守护模式：崩溃 / 卡死自动重启，可连同 go2rtc 和云台桥一起拉起；其余参数原样传给 run')
    p.add_argument('--no-stack', action='store_true', help='不拉起 go2rtc / xiaomi-onvif')
    p.add_argument('--events-dir', default=ev_default)
    p = sub.add_parser('health', help='执事是否在岗')
    p.add_argument('--events-dir', default=ev_default)
    p.add_argument('--max-age', type=float, default=300, help='心跳超过多少秒算掉线')
    p = sub.add_parser('check', help='检查视频流和模型')
    p.add_argument('--source', default=cfg('RTSP_URL'))
    p.add_argument('--image', default='')
    p.add_argument('--frames', type=int, default=60)
    p.add_argument('--det', default=os.path.join(models, 'yolov8n.onnx'))
    p.add_argument('--pose', default=os.path.join(models, 'yolov8n-pose.onnx'))
    sub.add_parser('login', help='米家登录（参数：--force）', add_help=False)
    sub.add_parser('devices', help='查询米家设备，写入 mi_devices.json')
    sub.add_parser('find-camera', help='扫描局域网里的小米设备')
    sub.add_parser('ptz', help='手动转云台：left/right/up/down 秒数 | stop | test', add_help=False)
    p = sub.add_parser('report', help='生成日报')
    p.add_argument('day', nargs='?', default=None, help='YYYYMMDD，默认今天')
    p.add_argument('--events-dir', default=ev_default)
    p = sub.add_parser('cmd', help='给运行中的执事发指令，如：没事 / 收到 / 属实 / 状态')
    p.add_argument('text', nargs='+')

    # login / ptz 的参数交给各自模块解析；serve 不认识的参数原样转给 run
    argv = sys.argv[1:]
    if argv and argv[0] == 'login':
        from mibutler.camera.mi_login import main as login
        return login(argv[1:])
    if argv and argv[0] == 'ptz':
        from mibutler.camera.onvif import main as ptz
        return ptz(argv[1:])
    if argv and argv[0] == 'serve':
        args, rest = ap.parse_known_args(argv)
        return cmd_serve(args, rest)
    args = ap.parse_args(argv)
    if args.cmd is None:
        ap.print_help()
        return
    {'run': cmd_run, 'health': cmd_health, 'check': cmd_check, 'devices': cmd_devices,
     'find-camera': lambda a: __import__('mibutler.camera.discover', fromlist=['main']).main(),
     'report': cmd_report, 'cmd': cmd_send}[args.cmd](args)


if __name__ == '__main__':
    main()
