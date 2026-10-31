<div align="center">

# caregiving-eye · 米执事

**米执事（Mi Butler）——常驻家中的 AI 执事：一台小米云台摄像头，值守防火防盗，照护老人小孩，顺便记录猫的一天。本地推理，画面不出内网。**

> 名字与说话方式见 [docs/butler.md](docs/butler.md)；个人开源项目，与小米公司无关。

<p>
  <img src="https://img.shields.io/badge/python-3.9%2B-2563EB.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/inference-local-16A34A.svg" alt="Local inference">
  <img src="https://img.shields.io/badge/PTZ-ONVIF-7C3AED.svg" alt="ONVIF PTZ">
  <img src="https://img.shields.io/badge/%E6%89%A7%E4%BA%8B-%E7%B1%B3%E6%89%A7%E4%BA%8B-F59E0B.svg" alt="Mi Butler">
  <img src="https://img.shields.io/badge/license-MIT-111827.svg" alt="License">
</p>

</div>

不刷固件、不改硬件：用社区补丁版 go2rtc 直连小米摄像头的局域网私有协议出 RTSP 流，YOLO 在本地跑人/猫检测与跌倒启发式，云台通过 xiaomi-onvif 桥暴露成标准 ONVIF 接口——任何 NVR / Home Assistant / Frigate 都能直接接，不锁死在本项目。

**已实测机型**：小米智能摄像机3 云台版（`chuangmi.camera.069a01`，固件 5.3.1）。同协议（miss/cs2）的小米云台机理论上适用，命令细节可能因型号而异。

## 实测记录

| 能力 | 结果 | 验证方式 |
| --- | --- | --- |
| 视频流 | H265 2960×1666 实时出流 | OpenCV 解码 + 画面时间水印 |
| AI 检测 | 35 秒分析 333 帧，person 置信度 0.88，沙发/餐桌/椅子全识别 | 实时流实测 |
| 云台控制 | ONVIF ContinuousMove 右转约 15–20° | 命令前后帧对比（差异度 35.5，基线噪声 <1.5） |

## 架构

```
米家账号(云端取凭据) ──► go2rtc 补丁版 (xiaomi:// 私有协议直连)
                              │
                              ▼
                    RTSP rtsp://127.0.0.1:8554/living (H265)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        care_pipeline    xiaomi-onvif 桥    本地查看/录像
        YOLO 人/猫检测    ONVIF :8891
        跌倒启发式        ► 标准云台接口
        猫禁区报警
```

## 如何安装与使用

**0. 前置下载**（二进制不入库，见 [SincereXIA/xiaomi-onvif releases](https://github.com/SincereXIA/xiaomi-onvif/releases)）

| 文件 | 说明 |
| --- | --- |
| `go2rtc.exe` | ⚠️ 必须用该仓库的 **go2rtc-windows-amd64.exe（补丁版）**，官方原版没有小米 PTZ 端点（404） |
| `xiaomi-onvif.exe` | ONVIF 桥 |
| `models/yolov8n.onnx` `models/yolov8n-pose.onnx` | [ultralytics assets v8.4.0](https://github.com/ultralytics/assets/releases/tag/v8.4.0) |

**1. 配置私密信息**

```bash
cp .env.example .env    # 填入米家账号、摄像头 IP/DID、网段；.env 已 gitignore
pip install -r requirements.txt
```

**2. 防火墙（管理员，一次）**

```powershell
powershell -File scripts/install_firewall.ps1
```

摄像头的 CS2 握手回包来自动态端口，Windows 防火墙默认丢弃，这是最容易卡住新手的一步。

**3. 米家登录（生成凭据）**

```bash
python mi_login.py          # 首次可能触发短信验证码，交互输入
python pull_mi_devices.py   # 生成 mi_devices.json，核对摄像头 did
```

**4. 启动 + AI + 云台**

```bash
scripts\start_stack.bat
python care_pipeline.py --source rtsp://127.0.0.1:8554/living --no-display
python onvif_ptz.py right 1.2   # 右转约15-20°后自动停止
```

## 执事能力（值班制度）

按"家里有没有人"自动切换值守重点——**无人值守**（入侵跟踪存证、火灾/烟雾视听双确认、玻璃碎异响）、
**在家照护**（跌倒/久卧/滞留/儿童危险/突发疾病复合信号）、**宠物模式**（禁区拆家、活动热力图、日报）。
完整身份设定、话术与施工分期见 **[docs/butler.md](docs/butler.md)**，算法选型见 **[docs/algorithms.md](docs/algorithms.md)**。

## 限制与边界（诚实版）

- **跌倒检测是启发式**（宽高比骤变 + 中心骤降 + 倒地静止计时），阈值需要按你的机位实测校准（`care_pipeline.py` 的 `FALL_CFG`）。它**不能替代专业监护设备**，漏报误报都可能有。
- 云台命令必须在视频流活跃时发（有 RTSP 消费者拉流中），否则报 "no active connection"。照护管线常驻拉流，天然满足。
- 每次连接摄像头需联网一次换流密钥（`miss_get_vendor`），流本身走局域网——完全断网的纯局域网方案需要进一步逆向固件，本项目不做。
- 单会话验证有限：目前只在一台路由器、一个机型上跑通，其他拓扑（尤其摄像头回包经网关的场景）欢迎反馈 issue。

## 踩坑备忘（省你一天）

1. go2rtc 必须 ≥1.9.13 才有 `xiaomi:` 源；**要 PTZ 必须用 xiaomi-onvif 补丁版**（原版 v1.9.15 的 PTZ API 返回 404）。
2. go2rtc 配置 `xiaomi:` 的 key 是 **passToken（`V1:` 开头）**，填 serviceToken 会 401。
3. 小米云 API 的 RC4 是 **drop1024 变体**（丢弃前 1024 字节密钥流），标准 RC4 实现必失败；`mi_api.py` 内置纯 Python 版，不依赖 pycryptodome。
4. 云端 API 是国内域，requests 客户端 `trust_env=False`，否则被系统代理劫持。
5. 二次验证的 verify 请求参数必须放 **query string**，放 form body 会卡进"确认安全手机"JS 死循环。
6. HEVC 中途入流先花屏，等关键帧（预热读流约 12 秒）再分析。
7. 电机命令 `{"operation":N}`（1左2右3上4下5归位）每次一小步；自研 miss 客户端 auth 在部分固件上被静默无视——本项目直接用 ONVIF 桥，不与固件斗智。
8. 报警默认只写本地 `events/`；在 `.env` 配 `ALERT_WEBHOOK` 后推送到钉钉/企微机器人（文字+本地快照路径，视频不出内网）。

## 隐私与合规

- 推理全部本地执行；仓库永不包含账号、token、设备 ID、内网 IP、画面（见 `.gitignore`）。
- 请仅用于自己家中或已获明确同意的场合；看护对象（尤其老人）应知情。

## License

[MIT](LICENSE)
