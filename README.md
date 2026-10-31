# caregiving-eye 🎥🐾

把小米智能摄像机（云台版）改装成**家庭照护站**：本地视频流 + AI 实时检测（跌倒/宠物拆家）+ 云台自动扫描。全程本地分析，画面不出内网。已实测机型：**小米智能摄像机3 云台版**（`chuangmi.camera.069a01`，固件 5.3.1），理论上适用同协议的 miss/cs2 系小米云台机。

## 架构

```
米家账号(云端取凭据) ──► go2rtc (xiaomi:// 私有协议直连, 需社区补丁版)
                              │
                              ▼
                    RTSP rtsp://127.0.0.1:8554/living   (H265 2.5K)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        care_pipeline    xiaomi-onvif 桥    本地录像/查看
        (YOLO 人/猫检测   (ONVIF :8891
         + 跌倒启发式      ► 标准云台接口)
         + 猫禁区报警)
```

## 目录结构

| 文件 | 作用 |
|---|---|
| `care_pipeline.py` | AI 管线：人/猫/狗检测 + 跌倒判定 + 猫进禁区报警 + 事件落盘/webhook |
| `mi_api.py` | 小米云 API 客户端（纯 Python 实现 RC4-drop1024 签名，不依赖 pycryptodome） |
| `mi_login.py` | 米家账号登录（passToken 续期 / 密码 / 短信二次验证）→ 生成 `mi_creds.json` |
| `pull_mi_devices.py` | 拉取账号下全部设备（型号/ip/token），生成 `mi_devices.json` |
| `onvif_ptz.py` | 云台控制（ONVIF ContinuousMove/Stop） |
| `find_camera.py` | 局域网小米设备发现（ping 扫描 + ARP OUI + miio 广播） |
| `common_config.py` | 读本地 `config.json`（私有配置，gitignored） |
| `scripts/start_stack.bat` | 一键启动 go2rtc + ONVIF 桥 |
| `scripts/install_firewall.ps1` | 管理员安装防火墙入站豁免（CS2 握手需要） |

## 安装

### 0. 隐私约定（先读）

仓库**永不提交**：账号、token、设备 ID、内网 IP、画面截图。它们只存在于本地
`config.json` / `mi_creds.json` / `mi_devices.json`（全部在 `.gitignore`）。
开始前：`cp config.example.json config.json` 并填入你的真实值。

### 1. 下载二进制（两个关键件，不要用官方原版 go2rtc！）

| 组件 | 来源 | 说明 |
|---|---|---|
| `go2rtc.exe` | [SincereXIA/xiaomi-onvif releases](https://github.com/SincereXIA/xiaomi-onvif/releases) 的 **go2rtc-windows-amd64.exe** | 补丁版：含小米 PTZ API 端点；官方原版 v1.9.15 会 404 |
| `xiaomi-onvif.exe` | 同仓库 releases 的 xiaomi-onvif-windows-amd64.exe | ONVIF 桥：把小米云台暴露成标准 ONVIF |

放到仓库根目录。模型自动下载（首次运行 `care_pipeline.py` 前手动下也行）：

```
models/yolov8n.onnx        https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n.onnx
models/yolov8n-pose.onnx   https://github.com/ultralytics/assets/releases/download/v8.4.0/yolov8n-pose.onnx
```

### 2. Python 依赖

```
pip install -r requirements.txt   # Python 3.9+
```

### 3. 防火墙（管理员运行一次）

摄像头的 CS2 握手回包来自动态端口，Windows 防火墙默认丢弃：

```
powershell -File scripts/install_firewall.ps1
```

### 4. 米家登录

```
export MI_USER=你的账号 MI_PASS=你的密码
python mi_login.py        # 生成 mi_creds.json
python pull_mi_devices.py # 生成 mi_devices.json (含设备 did/token)
```

> 首次登录大概率触发短信二次验证（脚本自动发码，交互输入）。
> 个别账号还会要求浏览器确认一次"安全手机"——按脚本提示打开 URL 处理后重跑。

### 5. 配置并启动

编辑 `config.json`（摄像头 ip/did/model、网段、端口），然后：

```
scripts\start_stack.bat
python care_pipeline.py --source rtsp://127.0.0.1:8554/living --no-display --webhook <可选钉钉/企微机器人>
```

### 6. 云台

```python
from onvif_ptz import move
move('right', 1.2)   # 右转约15-20°, 自动停止
```

⚠️ **云台命令必须在视频流活跃时发**（有消费者在拉 RTSP），否则会报
"no active connection"。照护管线常驻拉流，天然满足。

## 踩坑备忘（省你一天）

1. **go2rtc 必须 ≥1.9.13 且要 PTZ 端点用 xiaomi-onvif 补丁版**；官方版没有 xiaomi 模块（<1.9.13）或缺 PTZ API（原版）。
2. go2rtc 配置的 `xiaomi:` key 是 **passToken（V1: 开头）**，不是 serviceToken（401 教训）。
3. 每次连接摄像头需联网换一次流密钥（miss_get_vendor），流本身走局域网。
4. HEVC 中途入流会花屏——等关键帧（预热读流 ~12s）再取帧分析。
5. 小米云 API 的 RC4 是 **drop1024 变体**（丢前 1024 字节密钥流），用标准 RC4 必失败；本仓库 `mi_api.py` 已内置纯 Python 实现。
6. 云端 API 是国内域，requests 客户端记得 `trust_env=False`，别让系统代理劫持。
7. 二次验证的 verify 请求参数必须放 **query string**，放 form body 会卡进"确认安全手机"JS 死循环。
8. 云台每次操作必跟 Stop：miss 电机命令 `{"operation":N}`（1左2右3上4下5归位）每发一次走一小步；若发连续指令务必有终止条件。

## 隐私与合规

- 所有 AI 推理在本地完成，视频流不出局域网；报警只推文字+本地快照路径。
- 跌倒检测是启发式（宽高比骤变 + 中心骤降 + 倒地静止），**不能替代专业监护设备**，阈值守则见 `care_pipeline.py` 的 `FALL_CFG`（需按你的画面实测校准）。
- 请勿用于他人住所/未经同意的场合。

## License

MIT
