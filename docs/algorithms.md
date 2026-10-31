# 照护算法设计：场景 × 算法 × 落地顺序

> 目标：把「一路 2.5K 云台流」变成一套分层的照护感知系统。原则：**规则先行、模型递进**——
> P0 层不需要任何训练，今天就能上线；模型层（音频/时序骨架）按需升级。

## 算力底盘（本机 i7-8750H + GTX 1060 6G）

| 组件 | 实测/预估 | 说明 |
| --- | --- | --- |
| YOLOv8n 检测 | ~30 fps (1060) | person/cat/dog，已上线 |
| YOLOv8n-pose | ~10 fps | 17 关键点，已上线 |
| ByteTrack 跟踪 | 忽略不计 | ultralytics 内置，纯关联算法 |
| YAMNet 音频 | CPU 即可 | 521 类 AudioSet，1s 窗口 |
| ST-GCN 骨架分类 | 极轻 | 输入是关键点序列，不是像素 |

**明确不做**：Transformer 视频模型（TimeSformer/X-CLIP）——1060 带不动，收益比不了骨架方案。

## 场景 × 算法矩阵

| # | 场景 | 算法 | 层级 | 训练需求 |
| --- | --- | --- | --- | --- |
| A1 | 跌倒检测 | 姿态状态机 → ST-GCN | P0→P2 | 无 → UR Fall 数据集 |
| A2 | 倒地久卧 | 倒地静止计时器（已有） | P0 | 无 |
| A3 | 卫生间久滞 | zone + dwell 计时 | P0 | 无 |
| A4 | 夜间徘徊/离床 | zone × 时段规则 | P0 | 无 |
| A5 | 起床异常（"10点还没起"） | 作息统计 + 偏差告警 | P1 | 无（自累积数据） |
| A6 | 长时间无人出现 | person absence 计时 | P0 | 无 |
| A7 | 呼救/哭声/呻吟/玻璃碎 | YAMNet 音频事件 | P1 | 无（预训练直接用） |
| B1 | 猫上桌/上沙发 | 禁区 zone + 冷却（已有） | P0 | 无 |
| B2 | 抓挠拆家 | 禁区 + 驻留 + 音频辅助 | P0→P1 | 少量标注（可选） |
| C1 | 跨帧身份连续 | ByteTrack（ultralytics 内置） | P0 | 无 |
| C2 | 状态连续性 | 姿态状态机（stand/sit/lie 转移图） | P0 | 无 |
| C3 | 作息偏离 | 每小时活动直方图 + 简单统计 | P1 | 自累积 |
| D1 | 云台自动跟踪 | bbox 偏移 → ONVIF ContinuousMove | P3 | 无 |
| D2 | 分区巡航+驻留 | 扫描调度器 + 检测驻留 | P3 | 无 |

## P0 规则层（无需训练，全部可解释可调参）

### C1 跟踪：ByteTrack

`model.track(source, persist=True, tracker="bytetrack.yaml")` 一行接入，
person/cat 各自拿到稳定 `track_id`——跌倒计时、滞留计时、活动统计全部挂在 **track_id** 上，
而不是裸检测框（裸框每帧跳动，任何连续性分析都会碎）。

### C2 姿态状态机（跌倒检测的核心升级）

v0 的宽高比规则在猫干扰、侧身坐、蹲下时都会误报。升级为**关键点几何 + 转移图**：

```
每帧对每个 person track：
  torso = 肩中点(5,6) 与 髋中点(11,12) 的连线
  angle = torso 与竖直方向夹角
  torso_len = |肩中 - 髋中|（像素）

状态判定（带迟滞，避免边界抖动）：
  angle < 30°  且 torso_len 正常  → STAND
  60°< angle<120° 且髋部低于膝盖  → SIT/LIE（结合臀部 y 位置区分）
  torso_len < 0.5×历史中位数 且 angle>60° → LYING（透视缩短）

转移规则（挂在 track_id 上）：
  STAND → LYING 且 Δt < 2s 且中心下坠 > 0.18×帧高  → FALL 事件（高优先级）
  LYING 持续 > 30min 且非睡眠时段                   → LONG_LYING 事件
  LYING 出现在卫生间 zone                            → 立即升级告警
```

迟滞 + 历史中位数（ track 自适应透视变化）是把误报率打下来的关键，
参数照旧集中在 `FALL_CFG`，用真实画面逐个校准。

### A3/A4/A6 定时器族（简单但最实用）

全部是 `zone ∩ track_id ∩ 时间窗` 的组合：卫生间滞留 >20min、
深夜 02:00-05:00 出现在卧室外、连续 8h 无 person 检出、白天超出 4h 无人——
每一类都是独立开关 + 独立冷却，事件结构沿用现有 `events/alerts.jsonl`。

## P1 音频层：YAMNet

视觉有死角（卧室门关着、夜灯全黑），**声音是照护的第二传感通道**：

- 模型：[YAMNet](https://tfhub.dev/google/yamnet/1)（MobileNetV1，AudioSet 521 类，
  预训练直接用，CPU 实时）。相关类原生覆盖：`Baby cry, infant cry`、`Screaming`、
  `Glass`、`Cat`/`Meow`、`Crying, sobbing`。
- 取流：go2rtc 的 xiaomi 源支持双向音频——相机麦克风 → go2rtc RTSP 音频轨 → 本地
  ffmpeg 解 PCM → 1s 窗口喂 YAMNet → 命中类别过阈值 → 事件。
- 融合规则：`哭声 + 画面无人在客厅` ≫ 单独音频告警；`玻璃碎 + 夜间` = 高优先级。
  音频事件与视觉事件在 `alerts.jsonl` 里做 30s 窗口关联。

## P2 时序骨架：ST-GCN 跌倒分类器（替换规则法的模型升级）

规则法的上限是"看起来像跌倒"；分类器学的是"跌倒的动作模式"（失重相、冲击相、静止相）：

- 数据集：[UR Fall Detection Dataset](https://fenix.ur.edu.pl/mkepski/ds/uf.html)
  （30 跌倒 + 40 日常）、UP-Fall（Zenodo 有 [3D 骨架版](https://zenodo.org/records/12773013)）、Le2i。
- 管线范式（社区成熟路线，参考
  [GajuuzZ/Human-Falling-Detect-Tracks](https://github.com/GajuuzZ/Human-Falling-Detect-Tracks)
  与 [16dina/fall-detection](https://github.com/16dina/fall-detection)）：
  `YOLOv8-pose 关键点 → 滑窗序列(30帧) → ST-GCN++ 二分类(跌倒/日常)`，
  另见 [UC Berkeley 2024 骨架跌倒检测报告](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2024/Archive/EECS-2024-115.pdf)。
- 部署：骨架分类器极轻（输入是 17×30 的坐标序列），1060 上毫秒级；
  与 P0 规则法**并联跑**——两者都报才升级为最高优先级，规则法单独报降级为"疑似"。
- 训练数据补充：日常用家里真实录像（脱敏骨架化后只留关键点坐标，**数据本身即脱敏**）。

## P3 云台联动

- **自动跟踪**：`dx = 目标bbox中心x - 画面中心x`，超死区（±10%宽）就
  `onvif_ptz.continuous_move(±0.3)` 脉冲式修正——即社区那篇
  "把摄像头变成自跟踪机器人"的思路，但走标准 ONVIF。
- **分区巡航**：调度器按预设角度序列扫（每 ~15° 一站，驻留 4s 检测），
  见人/见猫即驻留该站直到目标离开 30s。云台命令必须在拉流中发（现有纪律）。

## 落地顺序

| 阶段 | 内容 | 工作量 | 依赖 |
| --- | --- | --- | --- |
| P0-1 | ByteTrack 接入 + track_id 化重构 | 0.5 天 | 无 |
| P0-2 | 姿态状态机替换 v0 跌倒规则 | 1 天 | 真实画面校准 |
| P0-3 | 定时器族（滞留/夜间/无人）+ zones 配置 | 0.5 天 | 无 |
| P1-1 | go2rtc 音频轨 → YAMNet 事件 + 视听关联 | 1 天 | 无 |
| P1-2 | 作息统计（先攒 2 周数据再定阈值） | 0.5 天 | P0 上线 |
| P2 | ST-GCN 训练与并联部署 | 2-3 天 | 数据集下载 |
| P3 | 自动跟踪 + 巡航调度 | 1 天 | 无 |

## 参考

- [EltonCCL/Fall-Detection](https://github.com/EltonCCL/Fall-Detection)（UR Fall + 骨架动作识别）
- [GajuuzZ/Human-Falling-Detect-Tracks](https://github.com/GajuuzZ/Human-Falling-Detect-Tracks)（YOLO+AlphaPose+ST-GCN 经典范式）
- [16dina/fall-detection](https://github.com/16dina/fall-detection)（YOLOv8-pose 跌倒检测）
- [UR Fall Detection Dataset](https://fenix.ur.edu.pl/mkepski/ds/uf.html) ·
  [UP-Fall 3D 骨架版](https://zenodo.org/records/12773013)
- [YAMNet (TF Hub)](https://tfhub.dev/google/yamnet/1)（AudioSet 521 类声音事件）
- [Skeleton-based Fall Detection using ST-GCN (Berkeley EECS-2024-115)](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2024/Archive/EECS-2024-115.pdf)
