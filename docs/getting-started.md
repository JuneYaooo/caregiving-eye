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
| [yolov8n-pose.onnx](https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n-pose.onnx) | v2 姿态原型使用；缺失时回退到检测框比例规则 |

模型不随仓库分发。当前推理默认使用 ONNX Runtime 的 CPU 执行器，不要求 NVIDIA 显卡。

## 2. 接入摄像头

### 下载配套二进制

从 [xiaomi-onvif v0.1.0-beta.2](https://github.com/SincereXIA/xiaomi-onvif/releases/tag/v0.1.0-beta.2) 下载 Windows x64 的配套文件，重命名并放到**仓库根目录**：

| 下载文件 | 保存为 |
| --- | --- |
| `go2rtc-windows-amd64.exe` | `go2rtc.exe` |
| `xiaomi-onvif-windows-amd64.exe` | `xiaomi-onvif.exe` |

需要云台控制时必须使用这套补丁版；普通 go2rtc 版本不一定包含小米 PTZ 端点。上游配置说明见 [xiaomi-onvif](https://github.com/SincereXIA/xiaomi-onvif)。

### 登录与查询设备

```powershell
python mi_login.py
```

首次登录可能触发短信验证，按终端提示完成。成功后会生成本地 `mi_creds.json`，其中包含 `userId` 与 `passToken` 等凭据。

使用仓库已有的 `MiApi` 查询设备并保存到本地：

```powershell
python -c "import json; from pathlib import Path; from mi_api import MiApi, load_creds; Path('mi_devices.json').write_text(json.dumps(MiApi(load_creds()).device_list(), ensure_ascii=False, indent=2), encoding='utf-8')"
```

在 `mi_devices.json` 中找到摄像头的 `did`、`localip`、`model`，补入 `.env` 的 `CAMERA_DID`、`CAMERA_IP`、`CAMERA_MODEL`。当前仓库的 `MiApi` 登录查询流程使用国内米家服务；其他地区账号需自行适配。

`mi_creds.json` 和 `mi_devices.json` 都包含敏感信息，已被 Git 忽略，不要粘贴到 Issue。旧的 `pull_mi_devices.py` 依赖额外的 `micloud` 包，且不自动读取 `.env`；本指南使用上面的查询方式。

### 配置 go2rtc

在根目录新建 `go2rtc.yaml`，将下列占位值替换成自己的信息：

```yaml
api:
  listen: "127.0.0.1:1984"

rtsp:
  listen: "127.0.0.1:8554"

xiaomi:
  "YOUR_USER_ID": "YOUR_PASS_TOKEN"

streams:
  living:
    - "xiaomi://YOUR_USER_ID:cn@YOUR_CAMERA_IP?did=YOUR_DEVICE_ID&model=chuangmi.camera.069a01&subtype=2"
```

`YOUR_USER_ID` 与 `YOUR_PASS_TOKEN` 来自 `mi_creds.json`；后者是 `passToken`，不是 `serviceToken`。`YOUR_CAMERA_IP`、`YOUR_DEVICE_ID` 和 `model` 按设备清单填写，`cn` 表示国内区。配置结构参考 [上游示例](https://github.com/SincereXIA/xiaomi-onvif/blob/main/examples/xiaomi-onvif/go2rtc.yaml)。

`.env` 不会自动生成或同步 `go2rtc.yaml`，更换设备或凭据时要同步检查两处。此配置仅监听本机；接入其他机器上的 NVR 前需另行配置可信网络访问。

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

这里使用显式启动命令：现有 `scripts/start_stack.bat` 会切换到 `scripts/` 目录，不能直接找到按本指南放在根目录的二进制与配置。

Windows 防火墙需要允许实际取流进程的局域网通信，尤其是摄像头 CS2 握手的 UDP 回包。出现系统提示时，为 go2rtc 允许可信的专用网络访问。`scripts/install_firewall.ps1` 只添加 Python 的入站规则，不能替代 go2rtc 自身的网络放行。

## 3. 运行检测与提醒

### 先验证基础检测

在第三个终端进入仓库目录，使用安装依赖的 Python 环境运行：

```powershell
python care_pipeline.py --source rtsp://127.0.0.1:8554/living --no-display
```

程序会加载模型并预热视频。H.265 中途入流可能暂时花屏，等待关键帧后再判断。依赖使用 `opencv-python-headless`，因此保留 `--no-display`；需要图形预览时应改用带 GUI 支持的 OpenCV 包。

保持拉流，在另一个终端测试云台：

```powershell
python onvif_ptz.py right 1.2
python onvif_ptz.py stop
```

角度随机型、速度与持续时间变化，先观察实际转动范围。

### 体验 v2 执事原型

停止基础管线后再运行 v2：

```powershell
python care_v2.py --source rtsp://127.0.0.1:8554/living --no-display
```

v2 接入 `Butler` 模式引擎、IoU 跟踪、姿态与区域规则。常见配置：

| 配置 | 用途 |
| --- | --- |
| `BUTLER_NAME` / `OWNER_TITLE` | 执事名字与对使用者的称呼 |
| `MODE_SCHEDULE` | 值班日程，如 `23:00-07:00:night`；当前日程分支在未检出人时生效 |
| `ALERT_WEBHOOK` | 钉钉 / 企业微信机器人地址；留空只记录本地 |
| `ZONE_DWELL_MIN` | 区域久滞阈值，单位为分钟 |
| `MODELS_DIR` | 模型目录，默认 `models` |

初次调试建议保持默认的 `events/` 目录。当前 v2 的事件日志、快照和日报对目录配置的处理尚未完全统一。

### 标定区域

```powershell
Copy-Item zones.example.json zones.json
```

按实际画面像素坐标修改多边形，不要直接使用示例坐标：

- `cat_zones`：宠物禁区。
- `bathroom_zones`：需要统计停留时长的区域。
- `bedroom_zones`：用于夜间卧室外活动判断。

规则使用目标框中心点判断是否进入区域。示例画面为 2960 × 1666；分辨率、机位或云台朝向改变后应重新校准。可选的 `--follow` 会移动摄像头，不能假定移动后的固定区域仍然有效。

### 查看事件与日报

v2 默认将事件写入 `events/alerts.jsonl`，部分人形事件会生成本地快照。配置 Webhook 后推送事件文字，不上传图片；目前没有接收聊天回复的服务，`handle_command()` 只是供后续集成使用的 Python 入口。

```powershell
# 汇总今天的 v2 事件日志
python report.py
```

日报保存为 `events/report_YYYYMMDD.md`。基础管线的日志格式不同，不会自动进入这个日报。

当前宠物活动网格写为 `heatmap_YYYYMMDD.npy`，日报读取 `heatmap_pet_YYYYMMDD.npy`，衔接尚未完成；网格坐标也固定按 2960 × 1666 映射。请暂将日报用于事件计数，不把热力图或“无通报”文字当作完整值守结论。

## 常见问题

| 现象 | 优先检查 |
| --- | --- |
| 小米 PTZ 接口返回 `404` | 是否下载了 xiaomi-onvif 的配套补丁版 go2rtc |
| 登录或视频源返回 `401` | `xiaomi:` 下是否填入 `passToken`，凭据是否过期 |
| 云台提示 `no active connection` | 保持至少一个 RTSP 消费者拉流，再发指令 |
| 视频连接卡住 | 摄像头 IP、同网段互通、go2rtc 的 Windows 防火墙规则、UDP 回包 |
| 刚连接时画面破碎 | 等待 H.265 关键帧与读流预热，不要立即判定取流失败 |
| ONVIF 地址能访问但流名不对 | `-camera` 显式指定 `living`；省略流名时上游可能默认寻找 `living_4k` |
| 无法弹出预览窗口 | 默认安装的是 headless OpenCV，启动时使用 `--no-display` |
| 跌倒或区域事件不准确 | 先检查检测框、遮挡与区域标定，再调 `FALL_CFG`；这些规则仍是原型 |

<details>
<summary>协议与登录排错备忘</summary>

- 小米云 API 使用 RC4 drop1024 变体；仓库的 `mi_api.py` 包含对应实现。
- 云端请求客户端禁用了环境代理继承；遇到登录问题时一并检查网络与账号地区。
- 二次验证的 verify 参数需要放在 query string，直接放入表单可能导致验证循环。
- 部分固件会忽略自研 miss 客户端的电机命令；当前通过配套 ONVIF 桥控制云台。
- 每次连接需联网换取密钥，单纯关闭外网后无法保证重新连接。

</details>

[返回项目首页](../README.md) · [提交问题](https://github.com/JuneYaooo/caregiving-eye/issues)
