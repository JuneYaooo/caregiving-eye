# 米执事接入指南

[← 返回 README](../README.md)

这份指南按 Windows x64 编写。先跑通“视频 → 检测 → 手动云台”，再尝试执事模式与区域规则。电脑与摄像头需在可互通的局域网内，连接时仍需联网向米家换取流密钥。

## 1. 准备环境

### 克隆与安装

安装 Git 与 Python 3.9+，在 PowerShell 中执行：

```powershell
git clone https://github.com/JuneYaooo/caregiving-eye.git
cd caregiving-eye
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

如果系统禁止激活脚本，可以直接用 `.\.venv\Scripts\python.exe` 代替下文的 `python`，无需修改全局执行策略。

在 `.env` 中填写 `MI_USER`、`MI_PASS`；设备信息可在下一步查询后补齐。其余地址先保留示例中的本机默认值。

### 下载模型

从 [Ultralytics assets v8.4.0](https://github.com/ultralytics/assets/releases/tag/v8.4.0) 下载并放到仓库根目录的 `models/`：

| 文件 | 用途 |
| --- | --- |
| [yolov8n.onnx](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx) | 必需，人 / 猫 / 狗检测 |
| [yolov8n-pose.onnx](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n-pose.onnx) | v2 姿态原型使用；跌倒、突发不适、儿童判定都依赖它，缺失时回退到检测框比例规则 |
| `fall_stgcn.onnx`（可选，本机训练） | 骨架时序跌倒分类，和姿态规则并联研判；训练步骤见 [算法设计](./algorithms.md#训练约-40-分钟cpu-即可)，没有时只用姿态规则 |

模型不随仓库分发。当前推理默认使用 ONNX Runtime 的 CPU 执行器，不要求 NVIDIA 显卡。

#### 可选模型：按需下载，缺了对应能力会自动跳过

| 能力 | 来源 | 放到 `models/` 后的文件名 |
| --- | --- | --- |
| 火焰 / 烟雾 | [rabahdev/fire-smoke-yolov8n](https://huggingface.co/rabahdev/fire-smoke-yolov8n)（D-Fire 数据集，类别 `smoke, fire`，AGPL-3.0） | `fire-smoke-yolov8n.onnx` |
| 声音事件 | [audiomagic/yamnet-onnx](https://huggingface.co/audiomagic/yamnet-onnx)（Google YAMNet，AudioSet 521 类，Apache-2.0） | `yamnet.onnx` 与 `yamnet_class_map.csv` |

火焰烟雾模型只提供 `.pt` 权重，需要导出一次 ONNX（只在导出时需要 ultralytics）：

```powershell
python -m pip install ultralytics
yolo export model=best.pt format=onnx imgsz=640
Move-Item best.onnx models\fire-smoke-yolov8n.onnx
```

其他 YOLOv8 火焰烟雾模型也能用，在 `.env` 中按模型类别顺序改 `FIRE_CLASSES`。声音事件还需要系统能找到 `ffmpeg`，且 go2rtc 输出的流带音频轨。

想让多人交叉走动时的跟踪更稳，可以安装 [supervision](https://github.com/roboflow/supervision) 并设 `TRACKER=bytetrack`：

```powershell
python -m pip install "supervision>=0.20,<0.30"
```

## 2. 接入摄像头

### 下载配套二进制

从 [xiaomi-onvif v0.1.0-beta.2](https://github.com/SincereXIA/xiaomi-onvif/releases/tag/v0.1.0-beta.2) 下载 Windows x64 的配套文件，重命名并放到**仓库根目录**：

| 下载文件 | 保存为 |
| --- | --- |
| `go2rtc-windows-amd64.exe` | `go2rtc.exe` |
| `xiaomi-onvif-windows-amd64.exe` | `xiaomi-onvif.exe` |

需要云台控制时必须使用这套补丁版；普通 go2rtc 版本不一定包含小米 PTZ 端点。上游配置说明见 [xiaomi-onvif](https://github.com/SincereXIA/xiaomi-onvif)。

### 机型兼容

米执事不自己处理视频协议，取流完全依赖 go2rtc 的 `xiaomi://` 源，能不能接进来由上游决定。上游按协议把米家摄像头分成三档，完整名单见 go2rtc 的 [Known Xiaomi cameras](https://github.com/AlexxIT/go2rtc/issues/1982)：

| 档位 | 协议 | 典型机型 | 米执事的情况 |
| --- | --- | --- | --- |
| 直连 | CS2（2019 年以后的大部分机型） | 小米智能摄像机 3 云台版 `chuangmi.camera.069a01`、2 云台版 `039c01`、C200 `046c04`、C300 `72ac1`、C700 `81ac1`、`isa.camera.hlc6/hlc7` 等 | 本项目在 3 云台版上实测；同档其他机型按上游名单推断可用，云台控制还要看固件是否响应 ONVIF 桥 |
| 有限支持 | TUTK（上游的 `xiaomi/legacy` 或 `xiaomi/miss`） | 米家云台版 `chuangmi.camera.v2`、小白 `xiaobai`、IMILAB A1 `ipc019e`、大方 `isa.camera.df3`、小方 `isc5`、Aqara G2 `lumi.camera.gwagl01` | 上游按型号硬编码握手，只认白名单里的型号；画质、音频可能受限，本项目未实测 |
| 不支持 | 老 TUTK 固件，上游未适配 | 小白增强版 `chuangmi.camera.v6`、叮零猫眼 `loock.cateye.v01` | 日志报 `xiaomi: unsupported model`，换 `model=` 伪装也不行，需要等上游单独适配（v6 见 [go2rtc #2119](https://github.com/AlexxIT/go2rtc/issues/2119)） |

同一型号的不同固件可能走不同协议：`chuangmi.camera.v2` 和 `isa.camera.df3` 新固件走 miss、旧固件走 legacy，go2rtc 会自动回退。`model=` 必须填 `mi_devices.json` 里的真实型号，填成别的型号只会换一种报错。遇到名单之外的机型，先去上游名单里查，能接通的话欢迎回到本仓库补一行实测记录。

### 登录与查询设备

```powershell
python main.py login
```

首次登录可能触发短信验证，按终端提示完成。成功后会生成本地 `mi_creds.json`，其中包含 `userId` 与 `passToken` 等凭据。

使用仓库已有的 `MiApi` 查询设备并保存到本地：

```powershell
python main.py devices
```

在 `mi_devices.json` 中找到摄像头的 `did`、`localip`、`model`，补入 `.env` 的 `CAMERA_DID`、`CAMERA_IP`、`CAMERA_MODEL`。当前仓库的 `MiApi` 登录查询流程使用国内米家服务；其他地区账号需自行适配。

`mi_creds.json` 和 `mi_devices.json` 都包含敏感信息，已被 Git 忽略，不要粘贴到 Issue。

### 配置 go2rtc

在根目录新建 `go2rtc.yaml`，将下列占位值替换成自己的信息：

```yaml
api:
  listen: "127.0.0.1:1984"

rtsp:
  listen: "127.0.0.1:8554"

webrtc:
  listen: "127.0.0.1:8555"   # 不写这段时 go2rtc 默认监听所有网卡

xiaomi:
  "YOUR_USER_ID": "YOUR_PASS_TOKEN"

streams:
  living:
    - "xiaomi://YOUR_USER_ID:cn@YOUR_CAMERA_IP?did=YOUR_DEVICE_ID&model=YOUR_CAMERA_MODEL&subtype=2"
```

`YOUR_USER_ID` 与 `YOUR_PASS_TOKEN` 来自 `mi_creds.json`；后者是 `passToken`，不是 `serviceToken`。`YOUR_CAMERA_IP`、`YOUR_DEVICE_ID` 和 `YOUR_CAMERA_MODEL` 按设备清单填写，`cn` 表示国内区。配置结构参考 [上游示例](https://github.com/SincereXIA/xiaomi-onvif/blob/main/examples/xiaomi-onvif/go2rtc.yaml)。

`.env` 不会自动生成或同步 `go2rtc.yaml`，更换设备或凭据时要同步检查两处。此配置仅监听本机；接入其他机器上的 NVR 前需另行配置可信网络访问。注意 go2rtc 的 1984 管理接口没有密码，且能读到含 `passToken` 的配置，不要把它改成 `0.0.0.0`。

### 启动视频与云台桥

在仓库根目录打开两个 PowerShell 终端，分别运行并保持开启：

```powershell
# 终端 A：视频服务
.\go2rtc.exe -config .\go2rtc.yaml
```

```powershell
# 终端 B：ONVIF 桥；末尾 living 明确指定 RTSP 流名
.\xiaomi-onvif.exe -go2rtc=http://127.0.0.1:1984 -rtsp=rtsp://127.0.0.1:8554 -camera=living=127.0.0.1:8891,living
```

在浏览器打开 `http://127.0.0.1:1984` 检查 `living` 是否出流。H.265 在部分浏览器中无法直接预览，可以继续使用下一步的 OpenCV 管线检查。

调通以后不必每次开两个终端：`python main.py serve` 会自动拉起仓库根目录里的 `go2rtc.exe` 和 `xiaomi-onvif.exe`（参数同上），再启动执事，三者任何一个退出都会自动重启。只想启动视频和云台栈时，也可以双击 `scripts\windows\start_stack.bat`。

> [!IMPORTANT]
> `go2rtc.yaml` 里 `api` 和 `rtsp` 都写 `127.0.0.1`、云台桥写 `-camera=living=127.0.0.1:8891`，视频流和云台控制就只有本机能访问。如果写成 `:8554` 或 `0.0.0.0`，同一 Wi-Fi 下的任何设备都能直接看画面、转云台。确实需要让局域网里的 NVR 读取时，再给 RTSP 加上用户名和密码：
>
> ```yaml
> rtsp:
>   listen: ":8554"
>   username: "butler"
>   password: "换成一个长密码"
> ```
>
> 同时把 `.env` 的 `RTSP_URL` 改成 `rtsp://butler:密码@127.0.0.1:8554/living`。

Windows 防火墙需要允许实际取流进程的局域网通信，尤其是摄像头 CS2 握手的 UDP 回包。出现系统提示时，为 go2rtc 允许可信的专用网络访问。`scripts/windows/install_firewall.ps1` 只添加 Python 的入站规则，不能替代 go2rtc 自身的网络放行。

## 3. 运行检测与提醒

### 先验证基础检测

在第三个终端进入仓库目录，使用安装依赖的 Python 环境运行：

```powershell
python main.py check --source rtsp://127.0.0.1:8554/living
```

程序会读约 60 帧，打印分辨率、帧率、每帧推理耗时和检出的人 / 猫狗。H.265 中途入流可能暂时花屏，等待关键帧后再判断。也可以用 `--image 某张图.jpg` 只测模型。

保持拉流，在另一个终端测试云台：

```powershell
python main.py ptz right 1.2
python main.py ptz stop
```

角度随机型、速度与持续时间变化，先观察实际转动范围。

### 启动执事

```powershell
python main.py run --source rtsp://127.0.0.1:8554/living
```

执事接入 `Butler` 模式引擎、IoU 跟踪、姿态与区域规则。常见配置：

| 配置 | 用途 |
| --- | --- |
| `BUTLER_NAME` / `OWNER_TITLE` | 执事名字与对使用者的称呼 |
| `MODE_SCHEDULE` | 值班日程，如 `23:00-07:00:night`；当前日程分支在未检出人时生效 |
| `ALERT_WEBHOOK` | 可选的兼容转发服务地址；未配置转发服务时留空 |
| `ZONE_DWELL_MIN` | 区域久滞阈值，单位为分钟 |
| `ABSENCE_MIN` | 多少分钟没看到人就发一条“家中无人”记录 |
| `MODELS_DIR` / `EVENTS_DIR` | 模型目录与事件目录，默认 `models`、`events` |
| `ILL_LYING_SEC` / `ILL_STILL_SEC` / `STILL_ALERT_MIN` | 突发不适判断的倒卧、无动作阈值 |
| `HAS_CHILD` / `CHILD_HEIGHT_BAND` / `ADULT_NEAR` | 儿童危险判断，家里有孩子时设 `HAS_CHILD=1` |
| `CLIP_PRE_SEC` / `CLIP_POST_SEC` | 事件视频存证保存的前后秒数 |
| `PET_UNSEEN_HOURS` | 宠物模式下多久没看到宠物就提醒，0 为关闭 |

启动日志的 `[butler] 值班表` 与 `[butler] 能力` 两行会列出实际启用的任务和能力。用 `--no-audio`、`--no-clips` 可以单独关闭声音事件或视频存证。检测频率、催办、磁盘保留等调度参数见 [执事的大脑](./brain.md#7-调参入口)。

运行中可以随时查看 `events/status.json`：当前模式、警觉等级、各任务平均耗时与失败次数、拉流帧率、未结事件、磁盘占用。某项能力连续报错时只会暂停它自己，错误原因也记在这里。

`--source` 也可以填本地录像文件，按原始帧率循环播放。建议先录一段自家画面（走动、坐下、躺下、宠物上桌）回放，调好区域和阈值再接实时流。

### 各模式启用的能力

| 能力 | 无人值守 | 在家照护 | 宠物 | 夜间 |
| --- | :---: | :---: | :---: | :---: |
| 火焰 / 烟雾、报警器声 | ✓ | ✓ | ✓ | ✓ |
| 入侵 + 云台跟踪 + 存证 | ✓ | | ✓ | |
| 玻璃碎裂声 | ✓ | | ✓ | ✓ |
| 跌倒、倒地久卧 | ✓ | ✓ | ✓ | ✓ |
| 突发不适（多信号） | | ✓ | | ✓ |
| 儿童危险 | ✓ | ✓ | ✓ | ✓ |
| 卫生间久滞 | ✓ | ✓ | ✓ | ✓ |
| 作息偏离 | ✓ | ✓ | ✓ | ✓ |
| 夜间离开卧室 | | | | ✓ |
| 婴儿哭声 | ✓ | ✓ | ✓ | ✓ |
| 宠物禁区 | ✓ | ✓ | ✓ | ✓ |
| 宠物嘶叫、哀鸣 | ✓ | | ✓ | |
| 宠物 / 家人活动记录与热力图 | ✓ | ✓ | ✓ | ✓ |

儿童危险需 `HAS_CHILD=1`；声音类事件需要声音模型与 ffmpeg；火焰烟雾需要火焰模型。

### 标定区域

```powershell
Copy-Item zones.example.json zones.json
```

按实际画面像素坐标修改多边形，不要直接使用示例坐标：

- `cat_zones`：宠物禁区，进入即提醒。
- `pet_zones`：猫爬架、食盆、猫砂盆等，只统计宠物停留，不提醒。
- `danger_zones`：灶台、窗台、阳台、大门等儿童危险区。
- `rest_zones`：床、沙发等，躺在这里不算异常倒卧。
- `bathroom_zones`：停留过久提醒；在这里倒卧会加重突发不适判断。
- `bedroom_zones`：用于夜间卧室外活动判断。
- `entry_zones`：大门、玄关。标了以后，人最后从这里消失才通报“出门”，10 分钟后进入无人值守；从别处消失（去了镜头外的房间）不通报，`AWAY_INSIDE_SEC`（默认 60 分钟）没再出现才进入无人值守。不标则所有消失都按出门处理。框要画大一点，把走到门口的整个人包进去。

规则使用目标框中心点判断是否进入区域；儿童危险区同时看脚下位置。示例画面为 2960 × 1666；分辨率、机位或云台朝向改变后应重新校准。可选的 `--follow` 会移动摄像头，不能假定移动后的固定区域仍然有效。

### 查看事件与日报

执事默认将每条提醒写入 `events/alerts.jsonl`，结案的事件写入 `events/incidents.jsonl`（含持续时长、是否确认、快照与录像）。主人回复的“没事”记录在 `events/feedback.jsonl`，用于学习误报位置。重要事件会保存快照 `snap_*.jpg`，跌倒、入侵、火情、突发不适、儿童危险还会保存事件前后的短视频 `events/clips/clip_*.mp4`，路径记录在事件的 `clip` 字段。查看本地记录无需配置机器人。执事运行时会在本机开一个指令入口 `http://127.0.0.1:8766/command`（端口见 `COMMAND_PORT`），机器人把主人的回复 POST 到这里即可，请求头需带 `X-Butler-Token`（值在仓库根目录自动生成的 `.command_token`，或 `.env` 的 `COMMAND_TOKEN`）；也可以直接运行 `python main.py cmd 没事`。回复 `属实` 表示确认是真事件。

```powershell
# 汇总今天的事件与活动
python main.py report
# 汇总指定日期
python main.py report 20261031
```

执事每天 `REPORT_AT`（默认 21:30）自动生成日报并推送摘要。日报包含：

- 事件分类计数与最近的重要事件（附录像路径）。
- 宠物与家人的活动总结：出现次数、画面中时长、活跃时长、最活跃时段、各区域停留次数与时长、24 小时活跃度条。
- 热力图 `events/report_heatmap_pet_YYYYMMDD.png`、`report_heatmap_person_YYYYMMDD.png`，叠加在当天自动保存的无人底图上。

活动数据每 5 分钟落盘一次，程序重启会接着当天的数据继续累计。云台转动后热力图位置会错开，需要统计宠物活动时尽量固定机位。

### 可选：飞书、微信与 QQ 机器人提醒

米执事可与 **OpenClaw / Hermes / WorkBuddy / ZCode / Claude Code / Codex** 等 AI 工具联合使用，通过另行配置的事件转发服务与机器人，将提醒发送到飞书、微信或 QQ。本项目不内置这些平台的机器人、账号登录或消息渠道配置。

根据所选工具支持的插件、连接器或脚本配置消息渠道，再接入本地 `events/alerts.jsonl` 中的事件。接入方式可参考以下文档；具体渠道支持与发送限制以所用工具和平台为准：

| 工具 | 接入文档 |
| --- | --- |
| OpenClaw | [消息渠道](https://docs.openclaw.ai/channels) · [飞书](https://docs.openclaw.ai/channels/feishu) |
| Hermes | [消息网关](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/) · [飞书](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/feishu) · [微信](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin) · [QQ](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/qqbot) |

如使用 `ALERT_WEBHOOK`，需提供能接收 `{"msgtype":"text","text":{"content":"事件文字"}}` 的转发服务，并由该服务适配网关要求的请求格式和鉴权。它不是这些平台机器人的直接配置入口，也不能直接替换成 OpenClaw / Hermes 的 Webhook 地址。

## 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 小米 PTZ 接口返回 `404` | 是否下载了 xiaomi-onvif 的配套补丁版 go2rtc |
| 日志报 `xiaomi: unsupported model` | 机型不在上游 go2rtc 的支持名单里，见 [机型兼容](#机型兼容)；改 `model=` 伪装无效 |
| 登录或视频源返回 `401` | `xiaomi:` 下是否填入 `passToken`，凭据是否过期 |
| 云台提示 `no active connection` | 保持至少一个 RTSP 消费者拉流，再发指令 |
| 视频连接卡住 | 摄像头 IP、同网段互通、go2rtc 的 Windows 防火墙规则、UDP 回包 |
| 刚连接时画面破碎 | 等待 H.265 关键帧与读流预热，不要立即判定取流失败 |
| ONVIF 地址能访问但流名不对 | `-camera` 显式指定 `living`；省略流名时上游可能默认寻找 `living_4k` |
| 无法弹出预览窗口 | 默认安装的是 headless OpenCV，启动时使用 `--no-display` |
| 跌倒或区域事件不准确 | 先检查检测框、遮挡与区域标定，再调 `FALL_CFG`；这些规则仍是原型 |
| “疑似跌倒”太多或漏报 | 用 `python scripts/fall_gcn/try_video.py 录像.mp4` 看模型打分；误报多就调高 `FALL_GCN_TH` / `FALL_GCN_STRONG`，宁可误报不能漏报就设 `FALL_FUSION=either` |
| 作息提醒不准 | 前 7 天不会提醒；按家人习惯调 `ROUTINE_LATE_MIN`、`ROUTINE_LOW_RATIO`，或 `ROUTINE_ENABLE=0` 关闭 |
| 火焰烟雾误报 | 阳光、红色物体、灯光都可能误检；提高 `FIRE_SCORE`，或避开窗户直射画面 |
| 声音事件没反应 | `ffmpeg` 是否在 PATH；状态行的 `audio=` 分贝是否变化；go2rtc 流是否带音频 |
| 儿童判定不准 | 先填 `CHILD_HEIGHT_BAND`，调 `CHILD_LEG_RATIO`；侧身、坐着时不参与判定 |
| go2rtc 报 `unsupported model`（如 `chuangmi.camera.v6`） | 该型号为 miIO + 云端 P2P 机型，本地没有 CS2 直连通道，当前版本不支持；伪装成其它 `model` 实测会 panic 或拉流超时，完整实测记录见 [#1](https://github.com/JuneYaooo/caregiving-eye/issues/1) |

<details>
<summary>协议与登录排错备忘</summary>

- 小米云 API 使用 RC4 drop1024 变体；仓库的 `mibutler/camera/mi_api.py` 包含对应实现。
- 云端请求客户端禁用了环境代理继承；遇到登录问题时一并检查网络与账号地区。
- 二次验证的 verify 参数需要放在 query string，直接放入表单可能导致验证循环。
- 部分固件会忽略自研 miss 客户端的电机命令；当前通过配套 ONVIF 桥控制云台。
- 每次连接需联网换取密钥，单纯关闭外网后无法保证重新连接。

</details>

[返回项目首页](../README.md) · [提交问题](https://github.com/JuneYaooo/caregiving-eye/issues)
