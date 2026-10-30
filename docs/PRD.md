# 自动视频匿名化系统 PRD

> 返回 [README](../README.md) ｜ 版本：v3.0 ｜ 状态：已实现 v1.1 + v2 + v3

## 1. 产品概述

### 1.1 产品定位

构建一套 **目录监听驱动的批量视频隐私处理系统**。用户在 `input` 目录投放视频源文件（或通过 Web 上传），系统自动完成 **预检 → 压缩（可选）→ 分段 → 静态丢弃 → 人脸/车牌匿名化 → 合并（可选）**，并输出到 `output` 目录，全程无需人工干预。系统以 Docker 方式部署，提供 Web 前端用于配置、监控、统计和文件管理。

v3 关键变化：

- **匿名化引擎内联化**：`app/pipeline/blur/`，不再依赖外部 CLI。
- **分段并行 + batch 推理**：`anonymize_parallel_segments` + `anonymize_batch_size`。
- **GPU 显存水位监测**：`gpu_mem_ok()` 加入 `resource_ok()`。
- **多段输出全量展示**：API 返回 `output_files`，前端完整列出。

### 1.2 核心约束与设计原则

- **无人值守批处理**：文件到达即触发，流水线自动推进，失败可降级、可记录。
- **编码健壮性优先**：不信任扩展名，对每个输入文件 ffprobe 预检；视频编码器在启动时运行时探测（`h264_nvenc` / `h264_qsv` 实际可编码才用，否则回退 `libx264`）。
- **源文件只读、输出只增**：`input/` 源文件与 `output/` 最终结果不因流水线失败而删除或修改。
- **中间文件即用即清**：任务成功后立即清理该源文件在 `workdir/` 下的中间产物；失败保留供排查。
- **运行时配置热更新**：Web 端修改参数，新任务立即生效；`anonymize_parallel_segments` 因依赖 `asyncio.Semaphore` 在启动时创建，需重启生效。
- **硬件性能可调**：匿名化并行路数、批大小、推理尺寸、显存水位全部可配，允许按硬件实际情况调优。

## 2. 用户角色与使用场景

| 角色 | 场景 |
|---|---|
| 运维/操作员 | 批量投放到 `input/`（或 Web 上传），在前端查看任务进度、删除文件、调整配置 |
| 隐私合规人员 | 检查 output 产物是否满足匿名化要求，查看错误记录与统计摘要 |
| 性能调优者 | 根据显卡与 CPU 资源，调 `anonymize_parallel_segments` / `anonymize_batch_size` / `anonymize_imgsz` |

核心流程：投放/上传文件 → 自动预检 → 自动流水线处理 → 结果出现在 `output/` → 前端查看状态与下载。

## 3. 功能需求

### 3.1 目录监听与任务生成

`watchdog` 监听 `input/`。`on_created` 与 `on_moved` 都触发 `wait_until_stable`，每 `FILE_STABLE_CHECK_INTERVAL`（默认 2s）采样文件大小，连续两次相同即视为写入完成，写入 `tasks(status='queued')` 并入队。超过 `FILE_STABLE_MAX_WAIT`（默认 300s）仍未稳定则跳过并记 warning。

支持扩展名：`.mp4` `.mov` `.mkv` `.avi`。

### 3.2 输入预检（编码兼容性纠偏）

对每个入队文件执行 `ffprobe` 探测，若音轨 codec 属于 `amr_nb / dts / eac3 / truehd`，用 `-c:v <best_encoder> -c:a aac -movflags +faststart` 转封装到 `workdir/{stem}_prechecked.mp4`。否则原样透传。源文件始终只读。

`best_encoder` 由 `app/core/hardware.py` 在启动时探测，见 §4.4。

### 3.3 视频压缩

在预检之后、分段之前。意图是降低后续 AI 推理的像素量以加速匿名化。

| 条件 | 行为 |
|---|---|
| `video_compress_enabled=False` | 跳过，原路径透传 |
| 探测不到高度 | 跳过，记 warning |
| 视频高度 ≤ `min_resolution` | 跳过（避免无意义重编码） |
| 高度 > `min_resolution` | 等比缩放到目标高度，重编码到 `workdir/{stem}_compressed.mp4` |

编码器首选 `hardware.encoder_args()` 的选择；首选失败时自动用 `libx264` 重试一次。显式 `-pix_fmt yuv420p`，音频统一 `aac 128k`，`-movflags +faststart`。

### 3.4 视频分段

`segment_enabled=True` 时按 `segment_duration` 分段，优先 `-c copy`；失败回退重编码。`segment_enabled=False` 时整文件作为单段。

分段产物在 `workdir/{stem}_segments/`（未压缩）或 `workdir/{stem}_compressed_segments/`（压缩后）。

### 3.5 静态处理

由 `static_process_enabled`（默认开启）控制。开启时，对每个分段计算 `process_static(seg)`（用 `mpdecimate` 保留的帧数 / 总帧数得到 `frame_ratio`）。若 `frame_ratio < static_process_threshold`（默认 **0.4**），判定为静态，`Segment.status='dropped'`，不进入匿名化；否则 `status='kept'`。

关闭时：跳过 mpdecimate 与 count_frames，所有分段直接 `status='kept'`、`frame_ratio=NULL`，不进入 `static_processing` 状态。

### 3.6 人脸/车牌匿名化（v3 重写）

#### 3.6.1 架构

不再调用外部 `privacy-blur` 子进程，改为进程内引擎 `app/pipeline/blur/`：

```text
app/pipeline/blur/
├── __init__.py       # 导出 process_video
├── detectors.py      # PlateDetectorYOLO / FaceDetectorYOLO（含 batch_call）/ FaceDetectorHaar
├── blur_ops.py       # gaussian_inplace / pixelate_inplace
├── utils.py          # choose_device / expand_box / nms_merge
└── engine.py         # 主循环 + thread-local 模型缓存 + batch 攒帧
```

`app/pipeline/anonymize.py` 调 `blur.engine.process_video(input_path, output_path, settings)`，保留 `tenacity` 重试（`ANONYMIZE_MAX_RETRIES` 默认 3）。**CUDA OOM 不做重试**，直接抛出（重试无意义且浪费时间）。

#### 3.6.2 模型缓存

`engine.py` 的 `_tls.cache` 按线程缓存 YOLO 模型。`_build_detectors` 在首次调用时加载模型并缓存，后续同线程复用。`cache_key` 包含 `(plate_weights, face_weights, device, conf, anonymize_plate, anonymize_face, FACE_DETECTOR)`，**不含 `imgsz` 与 `batch_size`**——切换这两个参数不触发模型重新加载。

**副作用**：任务结束后模型常驻显存（不主动 `del`），供后续任务复用。`nvidia-smi` 看到显存不回落是预期行为。

#### 3.6.3 batch 推理

`detectors.batch_call(frames, imgsz)` 一次喂 N 帧给 ultralytics。`engine.py` 的 `flush_batch` 攒帧规则：

- `anonymize_batch_size <= 1`：逐帧推理（与旧行为一致）。
- `anonymize_batch_size > 1`：攒到 N 帧或视频结尾时调用 `batch_call`。

#### 3.6.4 分段并行

`scheduler.py` 的匿名化阶段：

```python
async def _anon_one(idx, seg):
    out = WORKDIR / f"{stem}_anon_{idx:04d}.mp4"
    async with self.gpu_sem:                   # 全局信号量
        await asyncio.to_thread(anonymize, seg, out, settings)
    return idx, out

results = await asyncio.gather(*[_anon_one(i, s) for i, s in enumerate(kept)],
                               return_exceptions=True)
```

`self.gpu_sem = asyncio.Semaphore(anonymize_parallel_segments)` 在 `PipelineScheduler.start()` 创建，全任务共享。`MAX_WORKERS_PER_STAGE` 个任务同时进入匿名化时，总并发仍 = `anonymize_parallel_segments`，不会叠加。

#### 3.6.5 可调参数

| 配置 | 默认 | 范围 | 生效时机 |
|---|---|---|---|
| `anonymize_parallel_segments` | 2 | 1–16 | 需重启 |
| `anonymize_batch_size` | 1 | 1–16 | 新任务立即生效 |
| `anonymize_imgsz` | 640 | 320–1280 且为 32 倍数 | 新任务立即生效 |

调参建议：优先调 `batch`（不增 CPU 压力、不增模型副本），确认 GPU 利用率提升后再调 `parallel`。480p 压缩视频建议 `imgsz=480`。

### 3.7 视频合并与输出

| `anonymized` 段数 | `merge_enabled` | 输出 |
|---|---|---|
| 1 | 任意 | `output/{stem}_anonymized.mp4` |
| >1 | True | `output/{stem}_anonymized.mp4`（concat demuxer；codec 不一致时重编码） |
| >1 | False | `output/{stem}_anonymized_0000.mp4` … `_NNNN.mp4`（全部保留） |

**v3 多段展示**：`GET /api/tasks` 与 `GET /api/tasks/{id}` 在 API 层计算 `output_files` 字段：

- `_collect_output_files(task)` 从 `output_path` 文件名用正则 `^(?P<stem>.+)_anonymized_\d{4}\.mp4$` 提取 stem；
- 扫描 `OUTPUT_DIR` 匹配 `^{stem}_anonymized_\d{4}\.mp4$`，按序返回；
- 未命中时回退到 `output_path` 单文件；
- `failed` / 未完成任务返回 `[]`，不扫 `output/`（避免露出半成品）。

**不入库**：`output_files` 是 API 层动态计算的派生字段，不修改 `Task` 模型、无需迁移。

### 3.8 Input 文件管理

`GET /api/input-files` 列出 `input/` 所有文件，每项包含 `filename / size / mtime / status / task_id / error_message`。`status` 由该文件名最新任务推导（`unprocessed / processing / success / failed`）。

`DELETE /api/input-files/{filename}`：

- `processing`：`409`，前端按钮禁用
- `queued`：先标记任务 `failed` 再删文件
- 其余：直接物理删除

路径安全：只取 basename，`resolve()` 后校验父目录。

### 3.9 Output 文件管理

`GET /api/output-files` 列出文件。`DELETE /api/output-files/{filename}`：若存在 `status='merging'` 的任务则 `409`，否则物理删除。

### 3.10 运行配置在线编辑

- `GET /api/settings`：返回可编辑项 + 只读项
- `PUT /api/settings`：局部更新，**Pydantic 模型（`extra="forbid"`）** + `runtime_settings._validate` 双层校验
- 持久化到 `data/settings.json`（原子写 + `threading.RLock` + mtime 缓存）
- 生效时机：`anonymize_parallel_segments` 需重启，其余新任务生效

**v3 新增可编辑项**：`anonymize_parallel_segments`（1–16）、`anonymize_batch_size`（1–16）、`anonymize_imgsz`（320–1280，32 倍数）、`gpu_mem_threshold`（0.1–1.0）。

> 注意：`app/api/settings.py` 的 Pydantic 模型必须同步声明这些字段，否则 `extra="forbid"` 会返回 422。

### 3.11 中间文件清理

- 任务成功：`_cleanup_intermediates(stem)` 清理 `{stem}_prechecked.mp4`、`{stem}_compressed.mp4`、`{stem}_segments/`、`{stem}_prechecked_segments/`、`{stem}_compressed_segments/`、`{stem}_anon_*.mp4`、`{stem}_dedup_*.mp4`
- 任务失败：保留供排查
- 定时 cleaner（`CLEANUP_INTERVAL` 默认 3600s）：清理 `workdir/` 中 mtime 超过 `WORKDIR_MAX_AGE` 且不属于运行中任务的文件；清理 `input/.staging/` 中的 `.part` 残留

## 4. 技术架构

### 4.1 整体架构

```text
[watchdog] → tasks(queued)
                ↓
[PipelineScheduler] ← resource_ok() (psutil + GPU)
    ↓ 单队列 + 多 worker
[precheck] → [compress] → [segment] → [static_detect] → [anonymize(进程内)] → [merge]
    ↓
[cleaner]（定时清理 workdir 与 staging）
    ↓
[FastAPI + WebSocket] → 前端
[SQLite WAL] ← 统计/任务查询
```

### 4.2 流水线并发模型（v3 更新）

- **单队列 + 多 worker**：`asyncio.Queue(maxsize=QUEUE_MAXSIZE)`，`MAX_WORKERS_PER_STAGE` 个 worker 并发消费。变量名虽为 `MAX_WORKERS_PER_STAGE`，实际是 **全局任务级 worker 数**，不是分阶段独立设置。
- **资源闸**：`resource_ok()` = CPU<85% **且** 内存<85% **且** `gpu_mem_ok()`。
- **匿名化分段并行**：任务内用 `asyncio.gather` 并行多个分段，由 `self.gpu_sem`（全局 `asyncio.Semaphore`）限流。总并发 = `anonymize_parallel_segments`，不随任务数叠加。
- **各阶段阻塞调用**通过 `asyncio.to_thread` 放到线程池。

### 4.3 任务状态机

```text
queued → prechecking → compressing → segmenting → static_processing
       → anonymizing → merging → completed
                              ↘ failed
```

### 4.4 编码器运行时探测

`ffmpeg -encoders` 只表示编译期支持，不代表容器内 GPU 可用。`app/core/hardware.py` 在首次调用时用极短输入真实编码一次：

1. 探测源：`lavfi color=c=black:s=640x480:d=0.1`
2. 顺序：`h264_nvenc` → `h264_qsv` → `libx264`
3. `functools.lru_cache(maxsize=1)` 缓存结果

**v3 修复**：早期探测源为 `128x128`，NVENC 对输入分辨率有最小值要求，触发 `Frame Dimension less than the minimum supported value`，导致 NVENC 可用却误判为不可用。改为 `640x480` 后探测结果与实际任务一致。

**容器前置条件**：NVENC 需要 GPU 的 **`video` capability**，仅 `--gpus all` 只包含 `compute` 和 `utility`。启动时必须：

```bash
--gpus 'all,"capabilities=compute,utility,video"'
```

或 `-e NVIDIA_DRIVER_CAPABILITIES=all`。

### 4.5 GPU 显存监测（v3 新增）

`app/core/resources.py`：

```python
def gpu_mem_ratio() -> float | None:
    # 优先 torch.cuda.mem_get_info()，回退 pynvml / nvidia-smi
    # 2 秒缓存，避免频繁探测

def gpu_mem_ok(threshold: float | None = None) -> bool:
    # 无 GPU 环境返回 True（放行）
    # 阈值从 runtime_settings.gpu_mem_threshold 读取，默认 0.85

def resource_ok() -> bool:
    return cpu < 85 and mem < 85 and gpu_mem_ok()
```

## 5. 数据模型（SQLite，位于 `data/video_anonymizer.db`）

### 5.1 `tasks`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | UUID |
| `source_path` | String | 源文件绝对路径 |
| `output_path` | String | 输出绝对路径（`completed` 后写入；多段不合并时为 `_0000.mp4`） |
| `status` | String | `queued` / `prechecking` / `compressing` / `segmenting` / `static_processing` / `anonymizing` / `merging` / `completed` / `failed` |
| `error_message` | String | 失败原因摘要 |
| `retry_count` | Integer | 失败次数 |
| `created_at` / `started_at` / `finished_at` / `updated_at` | DateTime | |

### 5.2 `segments`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `task_id` | String FK → tasks.id | |
| `index` | Integer | 分段序号 |
| `path` | String | 分段文件路径 |
| `status` | String | `pending` / `kept` / `dropped` |
| `frame_ratio` | Float | 静态检测保留帧比例 |
| `created_at` | DateTime | |

### 5.3 `error_logs`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | Integer PK | |
| `task_id` | String FK | |
| `stage` | String | 失败阶段 |
| `message` | String | stderr 摘要 |
| `created_at` | DateTime | |

### 5.4 API 派生字段：`output_files`（v3 新增，不入库）

`GET /api/tasks` 与 `GET /api/tasks/{id}` 返回的每个任务对象额外包含：

```json
{
  "output_path": "/app/output/che1_anonymized_0000.mp4",
  "output_files": [
    "che1_anonymized_0000.mp4",
    "che1_anonymized_0001.mp4",
    "che1_anonymized_0002.mp4"
  ]
}
```

**计算规则**（`app/api/tasks.py::_collect_output_files`）：

1. 仅 `status == "completed"` 且 `output_path` 非空时扫描；
2. 用正则 `^(?P<stem>.+)_anonymized_\d{4}\.mp4$` 从 `output_path` 提取 stem；
3. 遍历 `OUTPUT_DIR` 匹配 `^{stem}_anonymized_\d{4}\.mp4$`，`sorted()` 后返回；
4. 未命中时回退 `[Path(output_path).name]`；
5. `failed` / 未完成 / `output_path` 为空时返回 `[]`。

**前端消费**：任务列表「输出」列显示 `_0000.mp4 等 N 个`（N>1 时），详情抽屉用 `<ul>` 列出全部文件，搜索框扩展至 `output_files`。

## 6. 前端交互规格

### 6.1 技术形态

原生 HTML + CSS + JavaScript 单页应用，无构建步骤。由 FastAPI 的 `StaticFiles(directory=BASE_DIR/"frontend", html=True)` 挂载到 `/`。

### 6.2 页面结构

| 区块 | 内容 |
|---|---|
| 顶部导航 | 品牌、WebSocket 状态徽章、刷新、主题切换 |
| 统计卡片 | 总任务 / 处理中 / 已完成 / 失败 / 丢弃分段 |
| 上传区 | 拖拽或点击选择，多文件，进度条 |
| 任务列表 | 表格，源文件 / 状态 / 进度 / 输出 / 创建时间 / 耗时 / 详情；输出列支持多文件展示 |
| Input 文件 | 列表 + 状态徽章 + 分页 + 删除 |
| Output 文件 | 列表 + 分页 + 删除 |
| 错误日志 | 时间倒序 |
| 运行配置 | 只读展示 + 「编辑」切换表单（含 v3 新增 4 项） |
| 任务详情抽屉 | 完整字段 + 输出文件列表 + 分段明细 + 错误记录 |

### 6.3 运行配置可编辑项（v3）

| 配置 | 类型 | 范围 |
|---|---|---|
| 启用分段 / 分段时长 | boolean / number | 1–7200 |
| 启用静态处理 / 阈值 / 丢弃静态帧 | boolean / number / boolean | 阈值 0–1 |
| 人脸 / 车牌匿名化 | boolean | |
| 模糊方式 | select | pixelate / gaussian |
| 启用合并 | boolean | |
| 启用视频压缩 / 目标分辨率 | boolean / number | 144–2160 |
| 匿名化并行数 | number | 1–16，重启生效 |
| 匿名化批大小 | number | 1–16，新任务生效 |
| 匿名化推理尺寸 | number | 320–1280，step 32，新任务生效 |
| GPU 显存水位阈值 | number | 0.1–1.0 |

### 6.4 实时推送

WebSocket `/ws/progress` 广播 `{task_id, status, output?, error?}`。前端收到后 `loadTasks()`、`scheduleFilesRefresh()`、`failed/completed` 时 `loadErrors()`。断线指数退避重连（1s → 30s）。

### 6.5 耗时口径

| 展示 | 计算 | 含义 |
|---|---|---|
| 表格「耗时」 | `started_at → finished_at` | 纯处理时间 |
| 表格「（排队 X）」 | `created_at → started_at` | 排队等待 |
| 抽屉「总耗时」 | `created_at → finished_at` | 端到端 |

## 7. 错误处理与健壮性

| 层级 | 当前实现 | 规划 |
|---|---|---|
| 预检 | ffprobe 失败 → `failed`；不兼容 → 转封装继续 | — |
| 压缩 | 首选编码器失败 → `libx264` 重试；两次都失败 → `failed` | — |
| 分段 | `-c copy` 失败 → 回退重编码；仍失败 → `failed` | 分段级 `corrupt` 状态 |
| 静态 | 判定失败按保留处理，继续 | — |
| 匿名化 | `tenacity` 重试；CUDA OOM 直接抛出不做重试；任一分段失败 → 任务 `failed` | 分段级容忍，允许部分成功 |
| 合并 | codec 一致 → `-c copy`；不一致 → 重编码 | concat filter 二级回退 |
| 清理 | 成功立即清理；失败保留；定时 cleaner 兜底 | `KEEP_FAILED_INTERMEDIATE` 开关 |
| 子进程超时 | 未实现 | `terminate → kill` 二级超时 |
| 磁盘水位 | 未实现 | 低于阈值暂停取新任务 |
| GPU 显存 | 已实现：`gpu_mem_ok()` 水位门控 | — |
| CUDA OOM | 已实现：不重试，直接失败 | 自动降 batch 重试 |

## 8. 统计

`GET /api/stats/summary` 返回 `tasks_total`、`tasks_failed`、`segments_dropped`。

**规划**（未实现）：`stats_daily` 预聚合、按日/周/月切换、按文件时长分桶、各阶段耗时占比、按 `blur_method` 分组、预检纠偏率、分段损坏率等。详见 §11。

## 9. 部署（Docker）

### 9.1 镜像

`Dockerfile` 基于 `python:3.12-slim`，安装 ffmpeg 与 `requirements.txt` 依赖（`ultralytics`、`opencv-python-headless`、`torch` 等）。不再安装 `vendor/video-privacy-blur`，匿名化源码已内联到 `app/pipeline/blur/`。

### 9.2 启动命令（CPU）

```bash
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

### 9.3 启动命令（GPU，v3 更新）

前置条件：NVIDIA 驱动 ≥ 535、Docker ≥ 23、`nvidia-container-toolkit` 已安装。

关键：NVENC 需要 `video` capability，只 `--gpus all` 不够：

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

等效：`-e NVIDIA_DRIVER_CAPABILITIES=all`。

验证：

```bash
docker exec -it auto_video_mark bash
ldconfig -p | grep libnvidia-encode          # 应看到 libnvidia-encode.so.1
ffmpeg -f lavfi -i color=c=black:s=640x480:d=1 -c:v h264_nvenc -f null -
```

### 9.4 持久化与恢复

`input/` `output/` `workdir/` `models/` `data/` 均通过卷挂载。`data/` 含 `video_anonymizer.db` 与 `settings.json`。

**恢复策略（当前）**：启动时只把 `status='queued'` 的任务重新入队。非终态任务不自动恢复，需人工介入。**规划**：按状态从对应阶段恢复。

## 10. 非功能需求

| 类别 | 当前状态 | 规划 |
|---|---|---|
| 编码健壮性 | 预检纠偏 + 压缩编码器回退 + NVENC 640x480 探测 | — |
| 匿名化性能 | 模型线程复用 + batch 推理 + 分段并行 + 显存水位 | 自动 batch 调优 |
| 前端响应 | WebSocket < 2s | 统计查询索引优化 |
| 磁盘 | 任务成功后立即清理中间文件 | 磁盘水位保护 |
| 数据安全 | 源文件只读；清理器不触碰 `input/` 与 `output/` | — |
| 可观测性 | `error_logs.stage` + `message` | `error_type` 分类 |

## 11. 风险与路线图

### 11.1 风险

| 风险 | 缓解 |
|---|---|
| `static_process_threshold` 默认 0.4 可能误丢缓慢运动视频 | 「运行配置」调低至 0.2/0.1 |
| `anonymize_parallel_segments` 过高导致 CPU/显存饱和 | `gpu_mem_ok()` 水位门控 + 建议从 2 起调 |
| 模型 thread-local 常驻导致显存不回落 | 预期行为；如需释放可手动 `empty_cache` |
| NVENC 探测失败（capability 缺失或探测源过小） | v3 已改 640x480；部署需 `capabilities=...video` |
| 非终态任务重启后不自动恢复 | 需人工排查；后续补"按状态恢复" |
| 失败任务中间文件保留占用磁盘 | 定时 cleaner 按 `WORKDIR_MAX_AGE` 清理 |

### 11.2 路线图

- [x] 目录监听 + 就绪检查
- [x] ffprobe 预检与编码纠偏
- [x] 视频压缩
- [x] 分段（含重编码回退）
- [x] 静态丢弃
- [x] 匿名化引擎内联化（移植 video-privacy-blur）
- [x] batch 推理 + 模型线程复用
- [x] 分段并行 + 全局 GPU 信号量
- [x] GPU 显存水位监测
- [x] 多段输出全量展示（`output_files`）
- [x] NVENC 探测分辨率修复（640x480）
- [x] 合并（含编码不一致回退）
- [x] 单队列 + 多 worker + 资源闸
- [x] 任务成功后立即清理中间文件
- [x] 前端：任务列表 / 上传 / 错误 / 运行配置在线编辑
- [x] 前端：Input/Output 文件管理
- [x] 前端：分页 / 时间筛选 / 简洁视图 / 耗时拆分
- [x] 前端：HTML/CSS/JS 拆分
- [x] 编码器运行时探测与 `libx264` 兜底
- [x] Docker GPU 部署（`capabilities=compute,utility,video`）
- [ ] 子进程超时（terminate → kill）
- [ ] 磁盘水位保护
- [ ] `stats_daily` 预聚合与统计图表
- [ ] `error_logs.error_type` 分类
- [ ] `KEEP_FAILED_INTERMEDIATE` 开关
- [ ] 非终态任务按阶段恢复
- [ ] 分段级 `corrupt` / `anonymize_failed` 容忍（允许部分成功）
- [ ] CUDA OOM 自动降 batch 重试
- [ ] HLS 精确分段
- [ ] 测试补齐与 CI