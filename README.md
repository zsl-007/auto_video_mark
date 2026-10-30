# 自动视频匿名化系统（Video Anonymizer）

目录监听驱动的批量视频隐私处理系统。将视频放入 `input` 目录（或通过 Web 上传），系统自动完成 **预检 → 压缩（可选）→ 分段 → 静态丢弃 → 人脸/车牌匿名化 → 合并（可选）**，结果输出到 `output` 目录。提供 Web 前端进行配置、任务监控、文件管理与错误查看，以 Docker 方式部署。

## 文档

- **[产品需求文档（PRD）](docs/PRD.md)**：功能规格、数据模型、流水线设计、错误处理、部署方案与路线图。开发前请通读。
- README（本文）：快速上手、目录结构、配置、常见问题。

---

## 功能特性

- **目录自动监听**：`input/` 投放文件即触发；文件写入稳定后自动入队；Web 上传通过临时文件 + `os.replace` 原子落盘。
- **编码健壮性**：ffprobe 预检，对 `amr_nb / dts / eac3 / truehd` 等不兼容音轨自动转 AAC；视频编码器在启动时 **运行时探测**（`h264_nvenc` / `h264_qsv` 实测可用才用，否则回退 `libx264`）。
- **视频压缩**：预检后、分段前按目标分辨率等比缩放，降低后续 AI 推理像素量；编码器失败自动用 `libx264` 重试。
- **可配置分段**：按 `segment_duration`（秒）分段；优先 `-c copy`，失败自动重编码；也支持不分段。
- **静态视频丢弃**：`mpdecimate` 保留帧比例低于 `static_process_threshold` 的分段被丢弃。
- **进程内匿名化引擎（v3）**：将 `video-privacy-blur` 的核心代码移植到 `app/pipeline/blur/`，不再依赖外部仓库与 CLI 子进程。支持 **模型线程本地复用**、**batch 推理**、**分段并行**，人脸与车牌可独立开关。
- **性能可调**：`anonymize_parallel_segments`（全局并发路数）、`anonymize_batch_size`（单路批大小）、`anonymize_imgsz`（推理分辨率）均可在前端在线编辑，新任务生效（并行数需重启）。
- **显存水位保护**：`resource_ok()` 在 CPU/内存之外新增 `gpu_mem_ok()`，显存超过 `gpu_mem_threshold` 时暂停取新任务。
- **可选合并**：编码一致时 `concat + -c copy`，不一致时重编码；关闭合并时多段全部输出，且 **任务列表与详情抽屉均完整展示所有分段文件**。
- **资源感知**：单队列 + 多 worker；取任务前检查 CPU/内存/显存水位，偏高时回队等待。
- **中间文件即用即清**：任务成功后立即清理 `workdir/` 中该源文件的中间产物；失败保留供排查，定时 cleaner 兜底。
- **前端完整控制台**：上传、任务列表（状态/时间/关键字筛选、分页、简洁视图、耗时拆分）、Input/Output 文件管理（分页、状态、删除）、运行配置在线编辑、错误日志、WebSocket 实时进度、明暗主题。

---

## 架构概览

```text
[watchdog] → tasks(queued)
                ↓
[PipelineScheduler] ← resource_ok() (psutil + GPU)
    ↓ 单队列 + 多 worker，匿名化阶段全局 gpu_sem
[precheck] → [compress] → [segment] → [static_detect] → [anonymize(进程内)] → [merge]
    ↓
[cleaner]（定时清理 workdir 与 upload staging）
    ↓
[FastAPI + WebSocket] → 前端（原生 HTML/CSS/JS）
[SQLite WAL] ← 任务/统计/配置查询
```

**任务状态机**：

```text
queued → prechecking → compressing → segmenting → static_processing
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
| 资源监控 | psutil + GPU 显存探测（torch / pynvml / nvidia-smi） |
| 媒体处理 | FFmpeg / ffprobe |
| 匿名化引擎 | 进程内（`app/pipeline/blur/`，移植自 video-privacy-blur，含 batch 推理） |
| 前端 | 原生 HTML + CSS + JavaScript（无构建） |
| 部署 | Docker（GPU 需 `capabilities=compute,utility,video`） |

---

## 目录结构

```text
.
├── app/
│   ├── main.py                 # FastAPI 入口 + lifespan
│   ├── api/
│   │   ├── tasks.py            # 任务列表/详情/分段 + output_files 富化
│   │   ├── errors.py
│   │   ├── stats.py
│   │   ├── settings.py         # GET/PUT /api/settings（Pydantic 校验）
│   │   ├── upload.py
│   │   └── files.py            # input/output 文件管理
│   ├── core/
│   │   ├── config.py           # 路径与常量
│   │   ├── runtime_settings.py # 运行时配置（data/settings.json）
│   │   ├── hardware.py         # 编码器运行时探测（640x480 探测源）
│   │   ├── logging.py
│   │   └── resources.py        # resource_ok() + gpu_mem_ok()
│   ├── db/
│   │   ├── models.py           # Task / Segment / ErrorLog
│   │   ├── session.py
│   │   └── migrate.py
│   ├── pipeline/
│   │   ├── scheduler.py        # 单队列 + 多 worker + 全局 gpu_sem
│   │   ├── watcher.py
│   │   ├── precheck.py
│   │   ├── compress.py
│   │   ├── segment.py
│   │   ├── static_process.py
│   │   ├── anonymize.py        # 调用 blur.engine.process_video
│   │   ├── blur/               # v3：进程内匿名化引擎
│   │   │   ├── __init__.py
│   │   │   ├── detectors.py    # YOLO 车牌/人脸检测（含 batch_call）
│   │   │   ├── blur_ops.py     # 高斯 / 马赛克
│   │   │   ├── utils.py        # device / box / nms
│   │   │   └── engine.py       # 视频循环 + 模型缓存 + batch 攒帧
│   │   ├── merge.py
│   │   └── cleaner.py
│   └── ws/
│       └── progress.py
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
- Docker 23+（部署）；GPU 需 NVIDIA 驱动 ≥ 535 + `nvidia-container-toolkit`
- `ultralytics` + `opencv-python-headless`（由 `requirements.txt` 安装）
- GPU 推理需 `torch` CUDA 版（`python -c "import torch; print(torch.cuda.is_available())"` 应为 `True`）

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

浏览器打开 `http://localhost:18000`。

### 2. 启用 GPU（可选，推荐）

前置条件：NVIDIA 驱动 ≥ 535、Docker ≥ 23、`nvidia-container-toolkit` 已安装。

**关键点**：只加 `--gpus all` **只包含 `compute` 和 `utility` 能力**，FFmpeg 的 NVENC 需要额外的 **`video` 能力**。启动命令必须显式声明：

```bash
docker run -d \
  --name auto_video_mark \
  --restart unless-stopped \
  --gpus 'all,"capabilities=compute,utility,video"' \
  -p 18000:8000 \
  -v "$(pwd)/input":/app/input \
  -v "$(pwd)/output":/app/output \
  -v "$(pwd)/workdir":/app/workdir \
  -v "$(pwd)/models":/app/models \
  -v "$(pwd)/data":/app/data \
  auto_video_mark:latest
```

等效替代：加环境变量 `-e NVIDIA_DRIVER_CAPABILITIES=all`。

确认编码器：

```bash
docker logs auto_video_mark 2>&1 | grep -i "编码器"
# 例：选用视频编码器: h264_nvenc   或  选用视频编码器: libx264
```

> **注意**：`hardware.best_encoder()` 的探测源已从 `128x128` 改为 `640x480`（NVENC 对输入分辨率有最小值要求，过小会误报不可用）。如果日志仍回退 `libx264`，请按 FAQ「NVENC 探测失败」排查。

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

前端「运行配置」→「编辑」→ 修改 →「保存」。变更落在 `data/settings.json`。

- **新任务生效**：`anonymize_batch_size`、`anonymize_imgsz` 等常规项。
- **需重启容器生效**：`anonymize_parallel_segments`（`asyncio.Semaphore` 在 `PipelineScheduler.start()` 时按配置创建）。

| 配置 | 默认 | 范围 / 说明 |
|---|---|---|
| 启用分段 | 是 | 关闭则整文件作为单段 |
| 分段时长 | 600 秒 | 1–7200 |
| 启用静态处理 | 是 | 关闭则跳过分段静态分析，所有分段直接进入匿名化 |
| 静态阈值 | 0.4 | 0–1；保留帧比例低于该值判定静态 |
| 人脸匿名化 | 是 | |
| 车牌匿名化 | 是 | |
| 模糊方式 | 马赛克 | `pixelate` / `gaussian` |
| 启用合并 | 是 | 关闭时多段分别输出，全部在列表展示 |
| 启用视频压缩 | 是 | |
| 目标分辨率 | 480 | 144–2160；源视频高度不超过该值时不压缩 |
| 匿名化并行数 | 2 | 1–16；全局同时运行的匿名化线程数（需重启） |
| 匿名化批大小 | 1 | 1–16；每线程一次推理的帧数（新任务生效） |
| 匿名化推理尺寸 | 640 | 320–1280，须为 32 的倍数；压缩后 480p 视频建议 480（新任务生效） |
| GPU 显存水位阈值 | 0.85 | 0.1–1.0；显存使用率超过该值时暂停取新任务 |

### 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `BASE_DIR` | `/app` | 数据根目录 |
| `SEGMENT_DURATION` | `600` | 分段时长（秒） |
| `SEGMENT_ENABLED` | `true` | |
| `STATIC_PROCESS_ENABLED` | `true` | 静态处理总开关 |
| `STATIC_PROCESS_THRESHOLD` | `0.4` | |
| `ANONYMIZE_FACE` | `true` | |
| `ANONYMIZE_PLATE` | `true` | |
| `BLUR_METHOD` | `pixelate` | |
| `MERGE_ENABLED` | `true` | |
| `VIDEO_COMPRESS_ENABLED` | `true` | |
| `MIN_RESOLUTION` | `480` | |
| `ANONYMIZE_PARALLEL_SEGMENTS` | `2` | 全局匿名化并发路数（运行时也可改，需重启） |
| `ANONYMIZE_BATCH_SIZE` | `1` | 单路批大小（运行时也可改，新任务生效） |
| `ANONYMIZE_IMGSZ` | `640` | YOLO 推理尺寸（运行时也可改，新任务生效） |
| `GPU_MEM_THRESHOLD` | `0.85` | 显存水位（运行时也可改） |
| `MAX_UPLOAD_SIZE` | `10737418240` | 字节 |
| `MAX_WORKERS_PER_STAGE` | `2` | 全局 worker 数（同时处理几个任务） |
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

> **`output_files` 不入库**：`GET /api/tasks` 与 `GET /api/tasks/{id}` 在 API 层按 `output_path` 的 stem 扫描 `output/` 下的 `{stem}_anonymized_*.mp4`，动态返回 `output_files` 列表。多段不合并时前端可完整展示并逐个下载。

---

## API 参考

```bash
# 配置
curl http://localhost:18000/api/settings | jq
curl -X PUT http://localhost:18000/api/settings \
  -H 'Content-Type: application/json' \
  -d '{"anonymize_parallel_segments":4,"anonymize_batch_size":8,"anonymize_imgsz":480}'

# 任务（返回中包含 output_files）
curl http://localhost:18000/api/tasks | jq
curl http://localhost:18000/api/tasks/<id>/segments | jq

# 文件
curl http://localhost:18000/api/input-files  | jq
curl http://localhost:18000/api/output-files | jq

# 统计
curl http://localhost:18000/api/stats/summary | jq
```

---

## 关键设计说明

### 编码器运行时探测

`ffmpeg -encoders` 只表示编译期支持；运行时真实编码一次才算可用。`hardware.best_encoder()` 用 **`640x480`** 黑帧试 `h264_nvenc` → `h264_qsv` → `libx264`，结果缓存。

> v3 修复：早期版本探测源为 `128x128`，NVENC 对输入分辨率有最小值要求（`Frame Dimension less than the minimum supported value`），导致 NVENC 明明可用却误判为不可用、回退 `libx264`。改为 `640x480` 后探测结果与实际任务一致。

### 进程内匿名化引擎（v3）

`app/pipeline/blur/` 移植自 `video-privacy-blur`，核心收益：

| 机制 | 说明 |
|---|---|
| 模型线程本地缓存 | `engine.py` 的 `_tls.cache` 按线程缓存 YOLO 模型，同一线程跨分段复用，避免每段重复加载 |
| batch 推理 | `detectors.batch_call` 一次喂 `anonymize_batch_size` 帧，显著提升 GPU 利用率 |
| 分段并行 | `scheduler.py` 用 `asyncio.gather` 并行多个分段，由全局 `gpu_sem` 限流 |
| imgsz 动态传入 | `cache_key` 不含 `imgsz` 与 `batch_size`，切这两个参数不重新加载模型 |

**并行数 vs 批大小**：

- `anonymize_parallel_segments`：任务级并发，同时有几个分段在跑；每路一份模型 + 一个解码器，主要消耗 CPU 核数、内存、显存（模型副本）。
- `anonymize_batch_size`：帧级并发，每个线程一次喂多少帧；主要消耗显存（激活值）和 GPU 计算单元。
- **调优顺序**：先调 `batch`（省 CPU、不增模型副本），GPU 利用率上不去再调 `parallel`。

**推荐组合**（480p 压缩后视频、YOLOv8n、`imgsz=640`）：

| GPU 显存 | `anonymize_batch_size` | `anonymize_parallel_segments` |
|---|---|---|
| 6–8 GB | 4 | 2 |
| 12 GB | 8 | 2 |
| 16 GB | 8 | 3 |
| 24 GB | 8 | 4 |

### 显存水位保护

`resources.gpu_mem_ok()` 优先用 `torch.cuda.mem_get_info()` 探测（容器内已有 torch），失败回退 `pynvml` / `nvidia-smi`，带 2 秒缓存。`resource_ok()` = CPU<85% **且** 内存<85% **且** 显存<`gpu_mem_threshold`。

### 静态阈值默认 0.4

保留帧比例低于 40% 判定为静态。若发现"有用内容被丢"，请在「运行配置」把 `static_process_threshold` 调低到 0.2 或 0.1。

### 输出命名规则

| 场景 | 输出 | `output_files` |
|---|---|---|
| 单段 | `output/{stem}_anonymized.mp4` | `[xxx_anonymized.mp4]` |
| 多段 + 合并 | `output/{stem}_anonymized.mp4` | `[xxx_anonymized.mp4]` |
| 多段 + 不合并 | `output/{stem}_anonymized_0000.mp4` … `_NNNN.mp4` | 全部 N 个 |

### 中间文件清理

任务成功后立即清理该 stem 在 `workdir/` 下的 `_prechecked`、`_compressed`、`_segments`、`_prechecked_segments`、`_compressed_segments`、`_anon_*.mp4`、`_dedup_*.mp4`。失败保留供排查，定时 cleaner 按 `WORKDIR_MAX_AGE` 兜底。**源文件与最终输出永不删除、永不修改**。

---

## 开发指南

### 新增一个处理阶段

1. 在 `app/pipeline/` 下新建模块，暴露纯函数
2. 在 `scheduler.py::_process` 插入该阶段
3. 前端 `app.js` 的 `STATUS_LABELS` / `STATUS_PROGRESS` / `RUNNING_STATUSES` 加新状态
4. `api/files.py::RUNNING_STATUSES` 加新状态
5. 若产生中间文件，在 `scheduler.py::_cleanup_intermediates` 补充清理模式

### FFmpeg 调用约定

```python
subprocess.run(["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", ...],
               capture_output=True, text=True)
```

失败时把 `stderr[-500:]` 写进 `ErrorLog.message`。编码统一走 `app/core/hardware.py::encoder_args()`。

### 测试

当前仓库未包含 `tests/`。集成测试建议覆盖：预检纠偏、压缩跳过与触发、静态判定、合并回退、`merge_enabled=False` 多段输出与 `output_files` 列表、匿名化 batch 与并行。

---

## 常见问题

**Q：任务失败，日志显示 `Could not open encoder before EOF / -22 Invalid argument`**

A：容器编译了 `h264_nvenc` 但 GPU 不可访问。`hardware.best_encoder()` 会自动回退 `libx264`；`compress` 再失败时也会用 `libx264` 重试一次。

**Q：NVENC 探测失败，日志显示 `Nothing was written into output file` 或 `Frame Dimension less than the minimum supported value`**

A：两个可能原因：

1. **探测源分辨率过小**：v3 已把探测源从 `128x128` 改为 `640x480`，如仍在旧版本请更新 `hardware.py`。
2. **容器缺少 `video` capability**：只加 `--gpus all` 不包含 NVENC 所需的 `video` 能力。启动时必须用 `--gpus 'all,"capabilities=compute,utility,video"'` 或 `-e NVIDIA_DRIVER_CAPABILITIES=all`。

**Q：`nvidia-smi` 显示的显存占用任务结束后不回落**

A：正常现象，不是泄漏。原因：① PyTorch 缓存分配器保留空闲块不归还 OS；② `engine.py` 的 thread-local 模型缓存让模型常驻线程，供下个任务复用。下一个任务会直接复用这些缓存，不影响批量任务。如需主动释放，可在 `_process` 的 `finally` 里加 `gc.collect()` + `torch.cuda.empty_cache()`（模型驻留部分仍不会释放）。

**Q：`output` 目录有多个文件，但任务列表只显示一个**

A：v3 已修复。API 层根据 `output_path` 的 stem 扫描 `output/` 返回 `output_files` 列表，列表显示 `xxx_anonymized_0000.mp4 等 N 个`，详情抽屉列出全部文件。

**Q：修改了 `anonymize_parallel_segments` 保存成功，但并发数没变**

A：该参数是全局 `asyncio.Semaphore`，在 `PipelineScheduler.start()` 时创建，**需重启容器**。其他参数（`batch_size`、`imgsz`、`gpu_mem_threshold`）对新任务立即生效。

**Q：并行处理两个视频时单任务耗时不降反升，总时间也没变少**

A：耗时统计是端到端时间，含排队 + 资源竞争。GPU 单卡并行在任务级不一定缩短总时间，**优先调 `anonymize_batch_size` 提升 GPU 利用率**，而不是无脑加 `MAX_WORKERS_PER_STAGE`。

**Q：上传后任务一直停留在 `queued`**

A：`docker logs` 应能看到「资源水位偏高」；调高 `RESOURCE_CHECK_INTERVAL`、降 `MAX_WORKERS_PER_STAGE`，或检查 `gpu_mem_threshold` 是否过低导致显存判断一直不通过。

**Q：静态丢弃把有用的缓慢运动视频丢了**

A：`static_process_threshold` 默认 0.4。改为 0.2 或 0.1 即可缓解。

---

## 路线图

见 [PRD 第 11 节](docs/PRD.md#11-风险与路线图)。近期优先级：子进程超时、磁盘水位保护、`stats_daily`、`error_logs.error_type`、非终态任务恢复。

---

## 参考

- [产品需求文档（PRD）](docs/PRD.md)
- [video-privacy-blur](https://github.com/MengWoods/video-privacy-blur)（源码已移植到 `app/pipeline/blur/`）
- [FFmpeg Documentation](https://ffmpeg.org/documentation.html)

---

## 许可证

待定（正式发布前补充 `LICENSE` 文件）。