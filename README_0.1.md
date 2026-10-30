# 自动视频匿名化系统（Video Anonymizer）

目录监听驱动的批量视频隐私处理系统。将视频放入 `input` 目录（或通过 Web 上传），系统自动完成**预检 → 压缩（可选）→ 分段 → 静态丢弃 → 人脸/车牌匿名化 → 合并（可选）**，结果输出到 `output` 目录。提供 Web 前端进行配置、任务监控、文件管理与错误查看，以 Docker 方式部署。

## 文档

- **[产品需求文档（PRD）](docs/PRD.md)**：功能规格、数据模型、流水线设计、错误处理、部署方案与路线图。开发前请通读。
- README（本文）：快速上手、目录结构、配置、常见问题。

---

## 功能特性

- **目录自动监听**：`input/` 投放文件即触发；文件写入稳定后自动入队；Web 上传通过临时文件 + `os.replace` 原子落盘。
- **编码健壮性**：ffprobe 预检，对 `amr_nb / dts / eac3 / truehd` 等不兼容音轨自动转 AAC；视频编码器在启动时**运行时探测**（`h264_nvenc` / `h264_qsv` 实测可用才用，否则回退 `libx264`）。
- **视频压缩（v2 新增）**：预检后、分段前按目标分辨率等比缩放，降低后续 AI 推理像素量；编码器失败自动用 `libx264` 重试。
- **可配置分段**：按 `segment_duration`（秒）分段；优先 `-c copy`，失败自动重编码；也支持不分段。
- **静态视频丢弃**：`mpdecimate` 保留帧比例低于 `static_threshold` 的分段被丢弃。
- **人脸 / 车牌匿名化**：集成 `video-privacy-blur`，支持马赛克 / 高斯模糊，人脸与车牌可独立开关；失败自动指数退避重试。
- **可选合并**：编码一致时 `concat + -c copy`，不一致时重编码；关闭合并时多段全部输出。
- **资源感知**：单队列 + 多 worker；取任务前检查 CPU/内存水位，偏高时回队等待。
- **中间文件即用即清**：任务成功后立即清理 `workdir/` 中该源文件的中间产物；失败保留供排查，定时 cleaner 兜底。
- **前端完整控制台**：上传、任务列表（状态/时间/关键字筛选、分页、简洁视图、耗时拆分）、Input/Output 文件管理（分页、状态、删除）、运行配置在线编辑、错误日志、WebSocket 实时进度、明暗主题。

---

## 架构概览

```
[watchdog] → tasks(queued)
                ↓
[PipelineScheduler] ← resource_ok() (psutil)
    ↓ 单队列 + 多 worker
[precheck] → [compress] → [segment] → [static_detect] → [anonymize] → [merge]
    ↓
[cleaner]（定时清理 workdir 与 upload staging）
    ↓
[FastAPI + WebSocket] → 前端（原生 HTML/CSS/JS）
[SQLite WAL] ← 任务/统计/配置查询
```

**任务状态机**：

```
queued → prechecking → compressing → segmenting → static_checking
       → anonymizing → merging → completed
                              ↘ failed
```

---

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12、FastAPI、Uvicorn |
| 数据 | SQLite（WAL）、SQLAlchemy |
| 目录监听 | watchdog |
| 资源监控 | psutil |
| 媒体处理 | FFmpeg / ffprobe |
| 匿名化引擎 | video-privacy-blur |
| 前端 | 原生 HTML + CSS + JavaScript（无构建） |
| 部署 | Docker |

---

## 目录结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口 + lifespan
│   ├── api/
│   │   ├── tasks.py            # 任务列表/详情/分段 + get_db
│   │   ├── errors.py
│   │   ├── stats.py            # /api/stats/summary
│   │   ├── settings.py         # GET/PUT /api/settings
│   │   ├── upload.py           # POST /api/upload
│   │   └── files.py            # input/output 文件管理
│   ├── core/
│   │   ├── config.py           # 路径与常量
│   │   ├── runtime_settings.py # 运行时配置（data/settings.json）
│   │   ├── hardware.py         # 编码器运行时探测
│   │   ├── logging.py
│   │   └── resources.py        # resource_ok()
│   ├── db/
│   │   ├── models.py           # Task / Segment / ErrorLog
│   │   ├── session.py
│   │   └── migrate.py
│   ├── pipeline/
│   │   ├── scheduler.py        # 单队列 + 多 worker
│   │   ├── watcher.py          # watchdog + 稳定性等待
│   │   ├── precheck.py
│   │   ├── compress.py         # v2
│   │   ├── segment.py
│   │   ├── static_detect.py
│   │   ├── anonymize.py
│   │   ├── merge.py
│   │   └── cleaner.py
│   └── ws/
│       └── progress.py         # WebSocket ConnectionManager
├── frontend/
│   ├── index.html
│   ├── css/style.css
│   └── js/app.js
├── input/                      # 挂载：源文件
├── output/                     # 挂载：最终产物
├── workdir/                    # 挂载：中间产物（自动清理）
├── models/                     # 挂载：YOLO 权重
├── data/                       # 挂载：video_anonymizer.db + settings.json
├── docs/PRD.md
├── requirements.txt
└── README.md
```

---

## 环境要求

- Python 3.12+（本地开发）
- FFmpeg + ffprobe（含 `libx264`；可选 `h264_nvenc` / `h264_qsv`）
- Docker 23+（部署）；可选 NVIDIA 驱动 + `nvidia-container-toolkit`（GPU）
- `privacy-blur` CLI 可用（`pip install` 或镜像内置）

---

## 快速开始

### 1. Docker（推荐）

```bash
docker build -t auto_video_mark:latest .
mkdir -p input output workdir models data

docker run -d \
  --name auto_video_mark \
  --restart unless-stopped \
  -p 18000:8000 \
  -v "$(pwd)/input":/app/input \
  -v "$(pwd)/output":/app/output \
  -v "$(pwd)/workdir":/app/workdir \
  -v "$(pwd)/models":/app/models \
  -v "$(pwd)/data":/app/data \
  auto_video_mark:latest
```

浏览器打开 `http://localhost:18000`。把视频拖入「上传视频」区域，或直接把文件复制到 `input/`。

### 2. 启用 GPU（可选）

前置条件：NVIDIA 驱动 ≥ 535、Docker ≥ 23、`nvidia-container-toolkit` 已安装。

```bash
nvidia-smi
docker run --rm --gpus all ubuntu nvidia-smi    # 应能输出 GPU 信息
```

启动加 `--gpus all`：

```bash
docker run -d \
  --name auto_video_mark \
  --restart unless-stopped \
  --gpus all \
  -p 18000:8000 \
  -v "$(pwd)/input":/app/input \
  -v "$(pwd)/output":/app/output \
  -v "$(pwd)/workdir":/app/workdir \
  -v "$(pwd)/models":/app/models \
  -v "$(pwd)/data":/app/data \
  auto_video_mark:latest
```

确认使用哪个编码器：

```bash
docker logs auto_video_mark 2>&1 | grep -i "编码器"
# 例：选用视频编码器: h264_nvenc   或  选用视频编码器: libx264
```

> **注意**：`--gpus all` 只解决设备透传，容器内 `ffmpeg` 必须编译了 NVENC。若 `ffmpeg -encoders | grep nvenc` 无输出，需要换用支持 NVENC 的 ffmpeg 镜像。系统会自动回退 `libx264`，任务不会因此失败。

### 3. 本地开发

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export BASE_DIR="$(pwd)"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 配置

### 在线编辑（推荐）

前端「运行配置」→「编辑」→ 修改 →「保存」。变更落在 `data/settings.json`，**只对之后入队的任务生效**（运行中任务使用创建时的快照）。

| 配置 | 默认 | 范围 / 说明 |
|---|---|---|
| 启用分段 | 是 | 关闭则整文件作为单段 |
| 分段时长 | 600 秒 | 1–7200 |
| 启用静态处理 | 是 | 关闭则跳过 mpdecimate 分析，所有分段直接进入匿名化 |
| 静态阈值 | 0.5 | 0–1；保留帧比例低于该值判定静态 |
| 人脸匿名化 | 是 | |
| 车牌匿名化 | 是 | |
| 模糊方式 | 马赛克 | `pixelate` / `gaussian` |
| 启用合并 | 是 | 关闭时多段分别输出 |
| 启用视频压缩 | 是 | 压缩阶段总开关 |
| 目标分辨率 | 480 | 144–2160；源视频高度不超过该值时不压缩 |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BASE_DIR` | `/app` | 数据根目录 |
| `SEGMENT_DURATION` | `600` | 分段时长（秒） |
| `SEGMENT_ENABLED` | `true` | |
| `STATIC_PROCESS_ENABLED` | `true` | 静态处理总开关 |
| `STATIC_THRESHOLD` | `0.5` | |
| `ANONYMIZE_FACE` | `true` | |
| `ANONYMIZE_PLATE` | `true` | |
| `BLUR_METHOD` | `pixelate` | |
| `MERGE_ENABLED` | `true` | |
| `VIDEO_COMPRESS_ENABLED` | `true` | v2 |
| `MIN_RESOLUTION` | `480` | v2 |
| `MAX_UPLOAD_SIZE` | `10737418240` | 字节 |
| `MAX_WORKERS_PER_STAGE` | `2` | 全局 worker 数（所有阶段共用） |
| `QUEUE_MAXSIZE` | `20` | |
| `RESOURCE_CHECK_INTERVAL` | `10` | 资源回队等待（秒） |
| `ANONYMIZE_MAX_RETRIES` | `3` | |
| `ANONYMIZE_RETRY_BACKOFF` | `2.0` | |
| `FILE_STABLE_CHECK_INTERVAL` | `2.0` | 就绪采样间隔（秒） |
| `FILE_STABLE_MAX_WAIT` | `300` | 就绪等待上限（秒） |
| `CLEANUP_INTERVAL` | `3600` | cleaner 巡检间隔（秒） |
| `WORKDIR_MAX_AGE` | `86400` | workdir 保留时长（秒） |
| `STAGING_MAX_AGE` | `3600` | 上传残留清理阈值（秒） |

`data/settings.json` 中的覆盖值优先于 `config.py` 的默认值。

---

## 数据模型（简述）

- **`tasks`**：`id` / `source_path` / `output_path` / `status` / `error_message` / `retry_count` / `created_at` / `started_at` / `finished_at` / `updated_at`
- **`segments`**：`task_id` / `index` / `path` / `status(pending|kept|dropped)` / `frame_ratio`
- **`error_logs`**：`task_id` / `stage` / `message`

完整字段与规划项（`stats_daily`、`error_type` 分类等）见 [PRD 第 5 节](docs/PRD.md#5-数据模型sqlite位于-datavideo_anonymizerdb)。

---

## API 参考

```bash
# 配置
curl http://localhost:18000/api/settings | jq
curl -X PUT http://localhost:18000/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"video_compress_enabled": true, "min_resolution": 480}'

# 任务
curl http://localhost:18000/api/tasks | jq
curl http://localhost:18000/api/tasks/<id>/segments | jq

# 文件
curl http://localhost:18000/api/input-files  | jq
curl http://localhost:18000/api/output-files | jq
curl -X DELETE http://localhost:18000/api/input-files/che1.mp4
curl -X DELETE http://localhost:18000/api/output-files/che1_anonymized.mp4

# 统计
curl http://localhost:18000/api/stats/summary | jq
```

---

## 关键设计说明

### 编码器运行时探测

`ffmpeg -encoders` 只表示编译期支持；运行时真实编码一次才算可用。`hardware.best_encoder()` 用 0.1s 黑帧试 `h264_nvenc` → `h264_qsv` → `libx264`，结果缓存。避免容器无 GPU 时在任务中途崩溃。

### 压缩

`compress.py` 只对"高度 > `min_resolution`"的视频触发；不满足时原样透传，零额外开销。首选编码器失败自动用 `libx264` 重试。

### 静态阈值默认 0.5

**v2 默认值较激进**：保留帧比例低于 50% 判定为静态。如果发现"有用内容被丢"，请在「运行配置」把 `static_threshold` 调低到 0.2 或 0.1。

### 输出命名规则

| 场景 | 输出 |
|---|---|
| 单段 | `output/{stem}_anonymized.mp4` |
| 多段 + 合并 | `output/{stem}_anonymized.mp4` |
| 多段 + 不合并 | `output/{stem}_anonymized_0000.mp4` … `_NNNN.mp4` |

### 中间文件清理

任务成功后立即清理该 stem 在 `workdir/` 下的 `_prechecked`、`_compressed`、`_segments`、`_prechecked_segments`、`_compressed_segments`、`_anon_*.mp4`。失败保留供排查，定时 cleaner 按 `WORKDIR_MAX_AGE` 兜底清理。**源文件与最终输出永不删除、永不修改**。

---

## 开发指南

### 新增一个处理阶段

1. 在 `app/pipeline/` 下新建模块，暴露类似 `compress(input_path, workdir, settings, stem) -> Path` 的纯函数
2. 在 `scheduler.py::_process` 插入该阶段：`task.status = "<状态>"` → `db.commit()` → `manager.broadcast(...)` → `asyncio.to_thread(...)`
3. 在 `tasks.py` 的 `STATUS_LABELS` 加新状态文案（前端 `app.js`）
4. 若前端需要状态筛选、进度映射、`RUNNING_STATUSES`，一并更新
5. 在 `api/files.py::RUNNING_STATUSES` 加入新状态，让 input 文件在这个阶段显示"处理中"
6. 若产生中间文件，在 `scheduler.py::_cleanup_intermediates` 补充清理模式

### FFmpeg 调用约定

当前项目直接 `subprocess.run`，不使用 shell，参数以 list 传入：

```python
subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", ...],
               capture_output=True, text=True)
```

失败时把 `stderr[-500:]` 写进 `ErrorLog.message`。编码统一走 `app/core/hardware.py::encoder_args()`。

### 测试

当前仓库未包含 `tests/` 目录（PRD §11 路线图已标注）。集成测试建议覆盖：预检纠偏、压缩跳过与触发、静态判定、合并回退、`merge_enabled=False` 多段输出。

---

## 常见问题

**Q：任务失败，日志显示 `Could not open encoder before EOF / -22 Invalid argument`**

A：容器编译了 `h264_nvenc` 但 GPU 不可访问。`hardware.best_encoder()` 会自动回退到 `libx264`；`compress` 再失败时也会用 `libx264` 重试一次。如果仍失败，确认 `ffmpeg -encoders | grep libx264` 存在，并检查 `docker logs` 中"选用视频编码器"打印的是哪个。

**Q：output 目录没有文件，workdir 里有 `xxx_anon_0000.mp4`**

A：v1.1 起已修复（单段会 `shutil.move` 到 `output/`）。旧数据可手动 `mv` 到 `output/` 并更新数据库 `output_path`。

**Q：并行处理两个视频时单任务耗时不降反升，总时间也没变少**

A：耗时统计是端到端时间，含排队 + 资源竞争。视频匿名化是重计算任务，并行通常不缩短总时间，GPU 单卡并行更应设为 1：

```bash
-e MAX_WORKERS_PER_STAGE=1
```

**Q：上传后任务一直停留在 `queued`**

A：`docker logs` 应能看到「资源水位偏高」；调高 `RESOURCE_CHECK_INTERVAL` 或降 `MAX_WORKERS_PER_STAGE`。

**Q：修改运行配置后，正在跑的任务没有生效**

A：设计如此。任务开始时读取配置，修改只影响之后入队的任务。

**Q：分段时长与设置不一致**

A：`segment` muxer 配合 `-c copy` 时切分点落在关键帧上，实际时长有浮动。需要精确时长需改用 HLS 分段（PRD 路线图）。

**Q：静态丢弃把有用的缓慢运动视频丢了**

A：`static_threshold` 默认 0.5 较激进。改为 0.2 或 0.1 即可缓解。

---

## 路线图

见 [PRD 第 11 节](docs/PRD.md#11-风险与路线图)。近期优先级：子进程超时、磁盘水位保护、`stats_daily`、`error_logs.error_type`、非终态任务恢复。

---

## 参考

- [产品需求文档（PRD）](docs/PRD.md)
- [video-privacy-blur](https://github.com/MengWoods/video-privacy-blur)
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)

---

## 许可证

待定（正式发布前补充 `LICENSE` 文件）。