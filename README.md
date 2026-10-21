# 自动视频匿名化系统（Video Anonymizer）

目录监听驱动的批量视频隐私处理系统。将视频放入 `input` 目录，系统自动完成**分段 → 静态视频丢弃 → 人脸/车牌匿名化 → 合并（可选）**，结果输出到 `output` 目录。提供 Web 前端进行配置、任务监控、报错查看与统计分析，以 Docker 方式部署。

## 文档

- **[产品需求文档（PRD）](docs/PRD.md)**：完整的功能规格、数据模型、流水线设计、错误处理策略、统计分析口径与部署方案。开发前请先通读。
- README（本文）：快速上手、目录结构、开发约定。

---

## 功能特性

- **目录自动监听**：`input` 目录投放文件即触发，文件写入完成后自动入队。
- **编码健壮性**：基于 `ffprobe` 预检，自动纠偏 MP4 不兼容的音视频编码（如 `amr_nb`、`dts`、`eac3` 等）。
- **可配置分段**：按用户设定时长分段（默认 10 分钟），流拷贝失败自动回退重编码；也支持不分段。
- **静态视频丢弃**：使用 `mpdecimate` 检测，按保留帧比例判定静态分段并丢弃（阈值可配）。
- **人脸 / 车牌匿名化**：集成 [`video-privacy-blur`](https://github.com/MengWoods/video-privacy-blur)，默认马赛克方法，可切换高斯模糊，人脸与车牌可独立开关。
- **可选合并**：编码一致时 concat + 流拷贝，不一致时自动重编码合并。
- **资源感知流水线**：阶段间有界队列 + 阶段内多 worker，依据 CPU/内存水位动态调节并发。
- **中间文件即用即清**：仅清理 `workdir` 中间文件；**源文件与最终输出永不删除、永不修改**。
- **前端可视化**：任务实时进度（WebSocket）、报错记录、详尽统计分析。

---

## 架构概览

```
[watchdog] → tasks(queued)
                ↓
[调度器] ← 资源监控 (psutil)
    ↓
[分段 Worker] → [静态检测 Worker] → [匿名化 Worker] → [合并 Worker]
    ↓ 有界队列（背压）
[清理器]（仅 workdir）
    ↓
[FastAPI + WebSocket] → Web 前端
[SQLite WAL] ← 统计查询
```

**任务状态机**：

```
queued → prechecking → segmenting → static_checking
       → anonymizing → merging → completed
                              ↘ failed
```

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12、FastAPI、Uvicorn |
| 数据 | SQLite（WAL 模式）、SQLAlchemy |
| 目录监听 | watchdog |
| 资源监控 | psutil |
| 媒体处理 | FFmpeg / ffprobe |
| 匿名化引擎 | video-privacy-blur（Ultralytics YOLO + OpenCV） |
| 前端 | 待定（建议 Vue/React + WebSocket） |
| 部署 | Docker / docker-compose |

---

## 目录结构（建议脚手架）

```
video-anonymizer/
├── app/
│   ├── main.py                 # FastAPI 入口
│   ├── api/                    # REST 路由
│   │   ├── tasks.py
│   │   ├── settings.py
│   │   ├── errors.py
│   │   └── stats.py
│   ├── ws/                     # WebSocket 推送
│   ├── core/
│   │   ├── config.py           # 环境变量 / 常量
│   │   ├── logging.py
│   │   └── resources.py        # CPU/内存/磁盘监控
│   ├── db/
│   │   ├── models.py           # tasks / segments / error_logs / stats_daily
│   │   ├── session.py
│   │   └── migrate.py
│   ├── pipeline/
│   │   ├── scheduler.py        # 任务调度与队列管理
│   │   ├── precheck.py         # ffprobe 预检与纠偏
│   │   ├── segment.py          # 分段
│   │   ├── static_detect.py    # 静态丢弃
│   │   ├── anonymize.py        # video-privacy-blur 调用
│   │   ├── merge.py            # 合并
│   │   └── cleanup.py          # workdir 清理（带安全边界）
│   ├── watcher/
│   │   └── input_watcher.py    # watchdog + 就绪检查
│   └── media/
│       └── ffmpeg_utils.py     # 子进程封装、超时、stderr 截取
├── frontend/                   # 前端源码
├── models/                     # YOLO 权重（卷挂载）
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── docs/
│   └── PRD.md                  # 产品需求文档
├── tests/
│   ├── unit/
│   └── fixtures/               # 含不兼容编码的样例视频
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## 环境要求

- Python 3.12+
- FFmpeg（含 ffprobe），系统需可调用 `ffmpeg` / `ffprobe`
- （可选）NVIDIA GPU + CUDA，用于加速 YOLO 推理
- Docker 24+ / docker-compose v2（部署时）

---

## 快速开始

### 1. 克隆与依赖

```bash
git clone <your-repo-url> video-anonymizer
cd video-anonymizer
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

安装匿名化引擎（本地开发）：

```bash
git clone https://github.com/MengWoods/video-privacy-blur.git ../video-privacy-blur
pip install -e ../video-privacy-blur
```

### 2. 准备目录与模型

```bash
mkdir -p input output workdir db models
# 将 YOLO 权重放入 models/（yolov8n-face.pt、license_plate_detector.pt 等）
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 按需修改路径与阈值
```

`.env.example` 关键项：

```dotenv
INPUT_PATH=./input
OUTPUT_PATH=./output
WORK_DIR=./workdir
DB_PATH=./db/tasks.sqlite
MODELS_DIR=./models

# 资源水位（百分比）
CPU_HIGH=85
CPU_LOW=60
MEM_HIGH=85
MEM_LOW=70

# 磁盘保护（GB）
DISK_LOW_GB=5
DISK_CRITICAL_GB=1

# 失败任务是否保留中间文件（排障用）
KEEP_FAILED_INTERMEDIATE=false
```

### 4. 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

打开 `http://localhost:8000`。向 `input/` 放入视频文件，观察任务自动推进。

### 5. Docker 部署

```bash
cd docker
docker compose up -d --build
```

默认挂载 `./input`、`./output`、`./workdir`、`./models`、`./db`。GPU 使用见 `docker-compose.yml` 中 `deploy.resources.reservations.devices` 配置；无 GPU 时删除该块并将设备设为 `cpu`。完整部署说明见 [PRD 第 9 节：部署方案](docs/PRD.md#9-部署方案docker)。

---

## 配置项（前端设置面板）

| 设置项 | 默认值 | 说明 |
|---|---|---|
| 是否分段 | 开 | 关闭则整文件作为单段 |
| 分段时长（分钟） | 10 | 1–120；实际时长受关键帧间隔影响 |
| 是否静态丢弃 | 开 | |
| 静态判定阈值（保留帧比例） | 0.05 | 低于该比例判定为静态 |
| 是否人脸匿名化 | 开 | |
| 是否车牌匿名化 | 开 | |
| 是否合并视频 | 关 | |
| 匿名化方法 | **马赛克** | 可选：马赛克 / 高斯模糊 |
| 设备 | auto | auto / cpu / cuda |
| 高级参数 | 工具默认值 | imgsz / conf / scale / blur-strength / pixelate-blocks / mpdecimate 阈值 |

设置变更仅对**之后入队**的任务生效；处理中任务使用创建时的 `settings_snapshot`。完整设置规格见 [PRD 第 6 节：前端交互规格](docs/PRD.md#6-前端交互规格)。

---

## 数据模型（简述）

- **tasks**：每个源文件一条，含状态、设置快照、预检信息、分段/静态/匿名化计数、输出路径、耗时。
- **segments**：分段级子状态（`segmented` / `static_dropped` / `anonymized` / `anonymize_failed` / `merged` / `corrupt`）。
- **error_logs**：按 `stage` + `error_type` 记录，含 stderr 摘要。
- **stats_daily**：按日预聚合，加速统计查询。

详细字段见 [PRD 第 5 节：数据模型](docs/PRD.md#5-数据模型sqlite)。

---

## 关键设计说明

设计细节、边界条件与失败兜底路径见 [PRD 第 3 节：功能需求](docs/PRD.md#3-功能需求) 与 [第 7 节：错误处理与健壮性](docs/PRD.md#7-错误处理与健壮性)。以下为开发时最常查阅的要点摘要。

### 编码纠偏策略

预检不兼容时：视频不兼容 → `-c:v libx264 -c:a aac`；仅音频不兼容 → `-c:v copy -c:a aac -ar 44100 -ac 2`。所有纠偏产物写入 `workdir`，源文件保持只读。

### 静态丢弃判定

比较输入分段与 `mpdecimate` 输出的**视频帧数**，保留帧比例低于阈值即判定为静态并丢弃整段（不做帧级裁剪）。

### 合并策略

分段编码参数一致 → concat demuxer + `-c copy`；不一致 → concat filter 重编码。失败自动降级，仍失败标记 `merge_failed`。

### 清理安全边界

清理器只删除 `workdir/{task_id}/` 下的文件。**input 源文件与 output 最终结果永不触碰**。清理失败写入 `error_logs(cleanup_failed)`，不阻塞终态。规则细节见 [PRD 3.7 中间文件清理](docs/PRD.md#37-中间文件清理)。

---

## 开发指南

### 新增一个处理阶段

1. 在 `app/pipeline/` 下创建模块，暴露 `handle(item)` 与阶段常量。
2. 在 `scheduler.py` 注册阶段与有界队列，定义上下游关系。
3. 在 `db/models.py` 扩展 `segments.status` 枚举（如需要）。
4. 补充 `error_logs` 的 `stage` / `error_type` 取值。
5. 前端任务详情增加对应子状态展示。

### FFmpeg 调用约定

- 统一通过 `app/media/ffmpeg_utils.py` 封装：参数列表式调用（避免 shell 注入）、可配超时、`terminate → kill` 兜底、stderr 截取末 500 字符入错误记录。
- 所有输出路径必须在 `workdir/{task_id}/` 下；工具函数内做路径校验。

### 测试

```bash
pytest tests/unit -q
```

`tests/fixtures/` 建议包含：正常 H.264/AAC、仅音频不兼容（如 AMR）、视频不兼容、损坏文件、超短静态片段等样例。集成测试覆盖"分段重编码回退""静态判定""合并回退"三类兜底路径。

---

## 路线图

- [ ] 目录监听 + 就绪检查
- [ ] ffprobe 预检与编码纠偏
- [ ] 分段（含重编码回退）
- [ ] 静态丢弃
- [ ] video-privacy-blur 集成（马赛克/高斯模糊）
- [ ] 合并（含 concat filter 回退）
- [ ] 流水线调度与资源自适应
- [ ] 中间文件清理
- [ ] Web 前端：设置 / 任务 / 报错 / 统计
- [ ] Docker 部署与恢复
- [ ] 测试补齐与 CI

---

## 常见问题

**Q：视频是 `.mp4` 后缀但处理报 "codec not supported in container"？**
A：这是预检纠偏的典型场景，系统会自动将不兼容音频转 AAC。若仍失败，检查 `workdir` 下的预检日志与 `error_logs`。策略见 [PRD 3.2 输入预检](docs/PRD.md#32-输入预检编码兼容性纠偏)。

**Q：分段时长与设置不一致？**
A：`segment` muxer 配合流拷贝时切分点落在关键帧上，实际时长会有浮动。需要精确时长可评估启用 HLS 分段方案。说明见 [PRD 3.3 视频分段](docs/PRD.md#33-视频分段)。

**Q：没有 GPU 能用吗？**
A：可以。`--device auto` 会回退到 CPU，吞吐下降。建议在纯 CPU 且核心充足时把匿名化阶段 `max_workers` 调至 2（需评估内存）。

**Q：失败任务的中间文件为何找不到？**
A：默认清理。设置 `KEEP_FAILED_INTERMEDIATE=true` 可临时保留，路径会写入报错记录。规则见 [PRD 3.7 中间文件清理](docs/PRD.md#37-中间文件清理)。

---

## 参考

- [产品需求文档（PRD）](docs/PRD.md)
- [video-privacy-blur](https://github.com/MengWoods/video-privacy-blur)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)

---

## 许可证

待定（建议在正式发布前确定并补充 `LICENSE` 文件）。