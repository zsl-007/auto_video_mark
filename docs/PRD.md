# 自动视频匿名化系统 PRD

> 返回 [README](../README.md) ｜ 版本：v2.0 ｜ 状态：已实现 v1.1 + 新增 v2 功能

## 1. 产品概述

### 1.1 产品定位

构建一套**目录监听驱动的批量视频隐私处理系统**。用户在 `input` 目录投放视频源文件（或通过 Web 上传），系统自动完成**预检 → 压缩（可选）→ 分段 → 静态丢弃 → 人脸/车牌匿名化 → 合并（可选）**，并输出到 `output` 目录，全程无需人工干预。系统以 Docker 方式部署，提供 Web 前端用于配置、监控、统计和文件管理。

### 1.2 核心约束与设计原则

- **无人值守批处理**：文件到达即触发，流水线自动推进，失败可降级、可记录。
- **编码健壮性优先**：不信任扩展名，对每个输入文件 ffprobe 预检；视频编码器在**启动时运行时探测**（`h264_nvenc` / `h264_qsv` 实际可编码才用，否则回退 `libx264`）。
- **源文件只读、输出只增**：`input/` 源文件与 `output/` 最终结果**不因流水线失败而删除或修改**；提供显式的文件删除 API/UI 用于运维手工清理。
- **中间文件即用即清**：任务成功后立即清理该源文件在 `workdir/` 下的中间产物；失败保留供排查，由 cleaner 按 `workdir_max_age` 兜底清理。
- **运行时配置热更新**：Web 端修改分段时长、静态阈值、压缩开关等，**只对之后入队的任务生效**；运行中任务使用任务开始时读取的配置快照。

## 2. 用户角色与使用场景

| 角色 | 场景 |
|---|---|
| **运维/操作员** | 批量投放到 `input/`（或 Web 上传），在前端查看任务进度、删除文件、调整配置 |
| **隐私合规人员** | 检查 output 产物是否满足匿名化要求，查看错误记录与统计摘要 |

核心流程：**投放/上传文件 → 自动预检 → 自动流水线处理 → 结果出现在 `output/` → 前端查看状态与下载**。

## 3. 功能需求

### 3.1 目录监听与任务生成

`watchdog` 监听 `input/`。`on_created` 与 `on_moved`（上传落盘的原子重命名）都触发 `wait_until_stable`，每 `FILE_STABLE_CHECK_INTERVAL`（默认 2s）采样文件大小，连续两次相同即视为写入完成，写入 `tasks(status='queued')` 并入队。超过 `FILE_STABLE_MAX_WAIT`（默认 300s）仍未稳定则跳过并记 warning。

支持扩展名：`.mp4` `.mov` `.mkv` `.avi`（`app/core/config.py::SUPPORTED_EXTENSIONS`）。

### 3.2 输入预检（编码兼容性纠偏）

对每个入队文件执行 `ffprobe` 探测，若音轨 codec 属于 `amr_nb / dts / eac3 / truehd`，用 `-c:v <best_encoder> -c:a aac -movflags +faststart` 转封装到 `workdir/{stem}_prechecked.mp4`。否则原样透传。**源文件始终只读**。

`best_encoder` 由 `app/core/hardware.py` 在启动时探测，见 §4.4。

### 3.3 视频压缩（v2 新增）

在预检之后、分段之前。意图是降低后续 AI 推理的像素量以加速匿名化。

| 条件 | 行为 |
|---|---|
| `video_compress_enabled=False` | 跳过，原路径透传 |
| 探测不到高度 | 跳过，记 warning |
| 视频高度 ≤ `min_resolution` | 跳过（避免无意义重编码） |
| 高度 > `min_resolution` | 等比缩放到目标高度（宽度按比例、取偶数），重编码到 `workdir/{stem}_compressed.mp4` |

编码器首选 `hardware.encoder_args()` 的选择；首选失败时**自动用 `libx264` 重试一次**（应对硬件编码器运行时不可用）。显式 `-pix_fmt yuv420p`，音频统一 `aac 128k`，`-movflags +faststart`。

### 3.4 视频分段

`segment_enabled=True` 时按 `segment_duration` 分段，优先 `-c copy`；失败回退重编码。`segment_enabled=False` 时整文件作为单段。

分段产物在 `workdir/{stem}_segments/`（未压缩）或 `workdir/{stem}_compressed_segments/`（压缩后）。

### 3.5 静态视频丢弃

对每个分段计算 `detect_static(seg)`：用 `mpdecimate` 保留的帧数 / 总帧数得到 `frame_ratio`。若 `frame_ratio < static_threshold`（默认 **0.5**，即保留帧比例低于 50%）判定为静态，`Segment.status='dropped'`，不进入匿名化；否则 `status='kept'`。

> 注意：默认阈值 0.5 相当激进，缓慢运动视频可能被误判。若出现"有用内容被丢"，请通过「运行配置」调低 `static_threshold`（如 0.2）。

### 3.6 人脸/车牌匿名化

调用 `privacy-blur` 子进程，参数由任务开始时的配置快照决定：

| 配置 | CLI 参数 |
|---|---|
| `blur_method` | `--method pixelate` / `--method gaussian` |
| `anonymize_plate` | 关闭时 `--no-blur-plates`；开启时 `--plate-weights models/license_plate_detector.pt` |
| `anonymize_face` | 关闭时 `--no-blur-faces`；开启且 `FACE_DETECTOR=yolo` 时 `--face-detector yolo --face-yolo-weights models/yolov8n-face.pt` |

失败重试由 `tenacity` 控制（`ANONYMIZE_MAX_RETRIES` 默认 3，`ANONYMIZE_RETRY_BACKOFF` 默认 2.0）。若某分段最终失败，`scheduler` 抛出后整任务 `failed`（当前实现为"任一分段失败则任务失败"，不做"部分成功"处理）。

### 3.7 视频合并与输出

| `anonymized` 段数 | `merge_enabled` | 输出 |
|---|---|---|
| 1 | 任意 | `output/{stem}_anonymized.mp4`（`shutil.move` 从 workdir 移到 output） |
| >1 | True | `output/{stem}_anonymized.mp4`（concat demuxer；codec 不一致时 `encoder_args()` 重编码合并） |
| >1 | False | `output/{stem}_anonymized_0000.mp4` … `_NNNN.mp4`（全部保留，不丢段） |

**v1.1 修复**：单段不再遗留在 `workdir`；`merge_enabled=False` 不再静默丢弃除首段外的其余分段。

### 3.8 Input 文件管理（v2 新增）

`GET /api/input-files` 列出 `input/` 所有文件，每项包含 `filename / size / mtime / status / task_id / error_message`。`status` 由该文件名最新任务推导：

| `Task.status` | 前端 `status` |
|---|---|
| 无任务 | `unprocessed` |
| `prechecking / compressing / segmenting / static_checking / anonymizing / merging` | `processing` |
| `completed` | `success` |
| `failed` | `failed` |
| `queued` | `unprocessed` |

`DELETE /api/input-files/{filename}`：

- `processing` 状态：后端返回 `409`，前端按钮禁用
- `queued`：先标记任务 `failed`（`error_message="源文件已被用户删除"`）再删文件，避免 worker 取到不存在的源
- 其余状态：直接物理删除

路径安全：只取 basename，`resolve()` 后校验父目录，杜绝 `../` 穿越。

### 3.9 Output 文件管理（v2 新增）

`GET /api/output-files` 列出文件。`DELETE /api/output-files/{filename}`：若存在 `status='merging'` 的任务则 `409`（避免删掉正在写入的产物），否则物理删除。

### 3.10 运行配置在线编辑（v2 新增）

- `GET /api/settings`：返回可编辑项 + 只读项（`max_upload_size` / `supported_extensions` / `input_dir` / `output_dir`）
- `PUT /api/settings`：局部更新，Pydantic + `runtime_settings._validate` 双层校验
- 持久化到 `data/settings.json`（原子写 + `threading.RLock` + mtime 缓存）
- **只影响之后入队的任务**；运行中任务使用创建时读取的快照

### 3.11 中间文件清理

- 任务成功：`_cleanup_intermediates(stem)` 清理 `{stem}_prechecked.mp4`、`{stem}_compressed.mp4`、`{stem}_segments/`、`{stem}_prechecked_segments/`、`{stem}_compressed_segments/`、`{stem}_anon_*.mp4`
- 任务失败：保留供排查
- 定时 cleaner（`CLEANUP_INTERVAL` 默认 3600s）：清理 `workdir/` 中 mtime 超过 `WORKDIR_MAX_AGE`（默认 86400s）且不属于运行中任务的文件；清理 `input/.staging/` 中超过 `STAGING_MAX_AGE`（默认 3600s）的 `.part` 上传残留

## 4. 技术架构

### 4.1 整体架构

```
[watchdog] → tasks(queued)
                ↓
[PipelineScheduler] ← resource_ok() (psutil)
    ↓ 单队列 + 多 worker
[precheck] → [compress] → [segment] → [static_detect] → [anonymize] → [merge]
    ↓
[cleaner]（定时清理 workdir 与 staging）
    ↓
[FastAPI + WebSocket] → 前端（原生 HTML/CSS/JS）
[SQLite WAL] ← 统计/任务查询
```

### 4.2 流水线并发模型

- **单队列 + 多 worker**：`asyncio.Queue(maxsize=QUEUE_MAXSIZE)`，`MAX_WORKERS_PER_STAGE` 个 worker 并发消费。**注意**：变量名虽为 `MAX_WORKERS_PER_STAGE`，实际是全局 worker 数（所有阶段共用），不是分阶段独立设置。
- **资源闸**：worker 取到任务后先 `resource_ok()`（CPU < 85% 且内存 < 85%），否则把任务放回队列并 `sleep(RESOURCE_CHECK_INTERVAL)`。
- 各阶段内通过 `asyncio.to_thread` 把阻塞的 ffmpeg/privacy-blur 子进程放到线程池，避免阻塞事件循环。

### 4.3 任务状态机

```
queued → prechecking → compressing → segmenting → static_checking
       → anonymizing → merging → completed
                              ↘ failed（任一阶段不可恢复）
```

### 4.4 编码器运行时探测（v2 新增）

`ffmpeg -encoders` 只表示编译期支持，不代表容器内 GPU 可用。`app/core/hardware.py` 在**首次调用时**用 0.1s 黑帧真实编码一次，验证运行时可用性：

1. 若 `h264_nvenc` 编译存在，先试 `h264_nvenc`
2. 失败则试 `h264_qsv`
3. 都失败回退 `libx264`
4. `functools.lru_cache` 缓存结果，避免每个任务重复探测

可选：`main.py::lifespan` 中预热（`await asyncio.to_thread(best_encoder)`），把探测提前到启动阶段。

## 5. 数据模型（SQLite，位于 `data/video_anonymizer.db`）

### 5.1 `tasks`

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String PK | UUID |
| `source_path` | String | 源文件绝对路径 |
| `output_path` | String | 输出绝对路径（`completed` 后写入） |
| `status` | String | `queued` / `prechecking` / `compressing` / `segmenting` / `static_checking` / `anonymizing` / `merging` / `completed` / `failed` |
| `error_message` | String | 失败原因摘要 |
| `retry_count` | Integer | 失败次数 |
| `created_at` | DateTime | 创建时刻 |
| `started_at` | DateTime | 开始处理时刻（v1.2 新增） |
| `finished_at` | DateTime | 终态时刻（v1.2 新增） |
| `updated_at` | DateTime | SQLAlchemy `onupdate` 自动刷新 |

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
| `stage` | String | 失败阶段（`precheck` / `compressing` / `segmenting` / … / `unknown`） |
| `message` | String | stderr 摘要 |
| `created_at` | DateTime | |

> 规划中：`stats_daily` 预聚合表、`error_logs.error_type` 分类、`segments.static_keep_ratio` 改名、`tasks.settings_snapshot` 显式快照字段。当前通过"任务开始时读取配置"实现隔离，未落库。

## 6. 前端交互规格

### 6.1 技术形态

原生 HTML + CSS + JavaScript 单页应用，无构建步骤。文件：

```
frontend/
├── index.html
├── css/style.css
└── js/app.js
```

由 FastAPI 的 `StaticFiles(directory=BASE_DIR/"frontend", html=True)` 挂载到 `/`。

### 6.2 页面结构

| 区块 | 内容 |
|---|---|
| 顶部导航 | 品牌、WebSocket 状态徽章、刷新、主题切换（明/暗） |
| 统计卡片 | 总任务 / 处理中 / 已完成 / 失败 / 丢弃分段 |
| 上传区 | 拖拽或点击选择，多文件，进度条，支持格式与上限提示 |
| 任务列表 | 表格，源文件 / 状态 / 进度 / 输出 / 创建时间 / 耗时 / 详情；支持状态、时间范围、关键字三重筛选；分页 10 条/页；简洁/详细视图切换 |
| Input 文件 | 列表 + 状态徽章 + 分页（8 条/页）+ 删除 |
| Output 文件 | 列表 + 分页（8 条/页）+ 删除 |
| 错误日志 | 时间倒序，阶段 + 消息 + 时间 |
| 运行配置 | 只读展示 + 「编辑」切换表单 |
| 任务详情抽屉 | 完整字段 + 分段明细 + 该任务错误记录 |

### 6.3 实时推送

WebSocket `/ws/progress` 广播 `{task_id, status, output?, error?}`。前端收到后：

1. `loadTasks()` 局部刷新
2. `scheduleFilesRefresh()`（800ms 节流）刷新 input/output 列表
3. `failed` / `completed` 时追加 `loadErrors()`

断线指数退避重连（1s → 30s 上限），`ws.onclose` 触发。

### 6.4 耗时口径

| 展示 | 计算 | 含义 |
|---|---|---|
| 表格「耗时」 | `started_at → finished_at`（回退 `created_at → updated_at`） | 纯处理时间 |
| 表格「（排队 X）」 | `created_at → started_at`（≥1s 才显示） | 排队等待 |
| 抽屉「总耗时」 | `created_at → finished_at` | 端到端 |

> 并行处理时单任务耗时不降反升是正常现象（排队 + 资源竞争），端到端总时间才是真实收益。详见 README FAQ。

## 7. 错误处理与健壮性

| 层级 | 当前实现 | 规划 |
|---|---|---|
| 预检 | ffprobe 失败 → 任务 `failed`；不兼容 → 转封装后继续 | — |
| 压缩 | 首选编码器失败 → `libx264` 重试；两次都失败 → 任务 `failed` | — |
| 分段 | `-c copy` 失败 → 回退重编码；仍失败 → 任务 `failed` | 分段级 `corrupt` 状态与损坏比例容忍 |
| 静态 | 判定失败按保留处理，继续 | — |
| 匿名化 | 任一分段失败 → 任务 `failed`（`tenacity` 内部重试） | 分段级 `anonymize_failed` 容忍，允许"部分成功" |
| 合并 | codec 一致 → `-c copy`；不一致 → 重编码 | concat filter 二级回退 |
| 清理 | 任务成功立即清理；失败保留；定时 cleaner 兜底 | `KEEP_FAILED_INTERMEDIATE` 开关 |
| 子进程超时 | **未实现**，由 ffmpeg 自身退出 | `terminate → kill` 二级超时 |
| 磁盘水位 | **未实现**，仅 cleaner 定期清理 | 低于阈值时暂停取新任务 |

> 未实现项均在 §11 路线图中跟踪。

## 8. 统计

当前 `GET /api/stats/summary` 返回 3 个字段：`tasks_total`、`tasks_failed`、`segments_dropped`。前端统计卡片直接消费。

**规划**（未实现）：`stats_daily` 预聚合、按日/周/月切换、按文件时长分桶、各阶段耗时占比、按 `blur_method` 分组的匿名化耗时对比、`precheck_info` 纠偏率、分段损坏率/失败率等。详见 §11。

## 9. 部署（Docker）

### 9.1 镜像

`Dockerfile` 基于 `python:3.12-slim`，安装 ffmpeg 与依赖，`pip install privacy-blur`（或本地源码）。模型通过 `models/` 卷挂载。

### 9.2 启动命令（推荐 `docker run`）

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
  -e SEGMENT_DURATION=600 \
  -e SEGMENT_ENABLED=true \
  -e STATIC_THRESHOLD=0.5 \
  -e ANONYMIZE_FACE=true \
  -e ANONYMIZE_PLATE=true \
  -e BLUR_METHOD=pixelate \
  -e MERGE_ENABLED=false \
  -e VIDEO_COMPRESS_ENABLED=true \
  -e MIN_RESOLUTION=480 \
  -e MAX_WORKERS_PER_STAGE=2 \
  -e QUEUE_MAXSIZE=20 \
  auto_video_mark:latest
```

### 9.3 GPU

前置条件：NVIDIA 驱动 ≥ 535、Docker ≥ 23、`nvidia-container-toolkit` 已安装。启动命令加 `--gpus all`。容器内 `ffmpeg` 必须编译了 NVENC；否则 `hardware.best_encoder()` 会自动回退 `libx264`（任务不会失败）。

### 9.4 持久化与恢复

`input/` `output/` `workdir/` `models/` `data/` 均通过卷挂载。`data/` 含 `video_anonymizer.db` 与 `settings.json`。

**恢复策略（当前）**：启动时只把 `status='queued'` 的任务重新入队。`prechecking` 等非终态任务**不会自动恢复**，需人工介入。**规划**：按状态从对应阶段恢复、中间文件可复用。

## 10. 非功能需求（当前达成 / 规划）

| 类别 | 当前状态 | 规划 |
|---|---|---|
| 编码健壮性 | 预检纠偏 + 压缩编码器回退 | — |
| 前端响应 | WebSocket < 2s（同机测试） | 统计查询索引优化 |
| 磁盘 | 任务成功后立即清理中间文件 | 磁盘水位保护 |
| 数据安全 | 源文件只读；清理器不触碰 `input/` 与 `output/`；文件名 basename 校验 | — |
| 可观测性 | `error_logs.stage` + `message` | `error_type` 分类、`precheck_info`、日志采样 |

## 11. 风险与路线图

### 11.1 风险

| 风险 | 缓解 |
|---|---|
| `static_threshold` 默认 0.5 可能误丢缓慢运动视频 | 通过「运行配置」调低至 0.2/0.1；后续把默认值改为 0.05 并补充分段预览 |
| 并行处理重任务时单任务耗时不降反升 | README FAQ 已说明；`MAX_WORKERS_PER_STAGE` 默认 2，GPU 单卡建议设 1 |
| 硬件编码器（nvenc/qsv）运行时不可用 | `hardware.py` 启动探测 + `compress.py` 二次回退 |
| 非终态任务在重启后不自动恢复 | 需人工排查；后续补"按状态恢复"逻辑 |
| 失败任务中间文件保留占用磁盘 | 定时 cleaner 按 `WORKDIR_MAX_AGE` 清理 |

### 11.2 路线图（按优先级）

- [x] 目录监听 + 就绪检查
- [x] ffprobe 预检与编码纠偏
- [x] 视频压缩（v2）
- [x] 分段（含重编码回退）
- [x] 静态丢弃
- [x] video-privacy-blur 集成
- [x] 合并（含编码不一致回退）
- [x] 单队列 + 多 worker + 资源闸
- [x] 任务成功后立即清理中间文件
- [x] 前端：任务列表 / 上传 / 错误 / 运行配置在线编辑
- [x] 前端：Input/Output 文件管理
- [x] 前端：分页 / 时间筛选 / 简洁视图 / 耗时拆分
- [x] 前端：HTML/CSS/JS 拆分
- [x] 编码器运行时探测与 `libx264` 兜底
- [x] Docker GPU 部署（`--gpus all`）
- [ ] 子进程超时（terminate → kill）
- [ ] 磁盘水位保护
- [ ] `stats_daily` 预聚合与统计图表
- [ ] `error_logs.error_type` 分类
- [ ] `KEEP_FAILED_INTERMEDIATE` 开关
- [ ] 非终态任务按阶段恢复
- [ ] 分段级 `corrupt` / `anonymize_failed` 容忍（允许部分成功）
- [ ] HLS 精确分段
- [ ] 测试补齐与 CI