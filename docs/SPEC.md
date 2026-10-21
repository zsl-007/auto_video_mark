# 自动视频匿名化系统 — 技术规格说明书（SPEC）

> 返回 [README](../README.md) ｜ 关联 [PRD](PRD.md) ｜ 版本：v0.1 ｜ 状态：评审中

## 0. 文档说明

### 0.1 定位与阅读顺序

- **PRD** 定义产品行为、默认值、业务规则与验收口径。
- **SPEC（本文）** 定义实现层面的数据结构、命令模板、状态机转移、接口协议、错误码、测试点与运行时约束。
- 开发前请按 **PRD → SPEC → README（脚手架/环境）** 的顺序阅读。
- 本文与 PRD 不一致处，以 PRD 的业务规则为准，SPEC 中相应位置需修订。

### 0.2 术语

| 术语 | 含义 |
|---|---|
| **任务（task）** | 一个源文件从入队到终态的完整处理记录，对应 `tasks` 一行 |
| **分段（segment）** | 一个任务切分出的处理子单元，对应 `segments` 一行；不分段时任务只有 1 个分段 |
| **工作子目录** | `workdir/{task_id}/`，任务所有中间文件的唯一存放位置 |
| **阶段（stage）** | 预检 / 分段 / 静态 / 匿名化 / 合并 / 清理，与 `error_logs.stage` 枚举一致 |
| **纠偏** | 预检阶段对编码不兼容文件执行的转码/重封装动作 |

### 0.3 实现语言与关键依赖（锁定版本区间）

| 依赖 | 用途 | 版本约束 |
|---|---|---|
| Python | 运行时 | `>=3.12,<3.13` |
| FastAPI / Uvicorn | HTTP + WS | `fastapi>=0.115`，`uvicorn[standard]>=0.30` |
| SQLAlchemy | ORM + DDL 管理 | `>=2.0` |
| watchdog | 目录监听 | `>=4.0` |
| psutil | 资源监控 | `>=5.9` |
| FFmpeg / ffprobe | 媒体处理 | 系统安装，`>=6.0` 建议（需支持 `-f segment`、`concat`、`mpdecimate`） |
| video-privacy-blur | 匿名化引擎 | 以仓库 `pyproject.toml` 为准，`pip install -e` |

---

## 1. 运行时模型

### 1.1 进程与线程布局

单容器单进程。启动时创建以下线程/协程：

| 组件 | 类型 | 职责 | 生命周期 |
|---|---|---|---|
| FastAPI 事件循环 | asyncio | HTTP / WS | 进程生命周期 |
| `InputWatcher` | 守护线程 | watchdog 监听 + 就绪检查入队 | 进程生命周期 |
| `Scheduler` | 守护线程 | 持有各阶段队列，启停 worker | 进程生命周期 |
| 各阶段 Worker | 线程（`threading.Thread`） | 执行 FFmpeg / CLI 子进程 | 由 Scheduler 动态增减 |
| `ResourceMonitor` | 守护线程 | psutil 采样，输出水位信号 | 每 `RESOURCE_SAMPLE_SEC`（默认 5s）采样一次 |
| `DiskGuard` | 守护线程 | 磁盘水位检查与暂停信号 | 每 `DISK_SAMPLE_SEC`（默认 30s）采样一次 |
| `CleanupWorker` | 守护线程 | 消费清理队列 + 终态兜底清理 | 进程生命周期 |

**选型说明**：媒体处理是阻塞式子进程调用，使用**线程 + 同步队列**而非 asyncio 协程，避免阻塞事件循环；Web 层与流水线之间通过 SQLite（WAL）与进程内事件总线解耦，WS 推送由独立发布逻辑完成。

### 1.2 流水线拓扑（与 PRD 4.1 一致）

```
queued
  │  [Scheduler 主循环：受 DiskGuard 暂停信号与 queued 队列控制]
  ▼
prechecking ──► segmenting ──► static_checking ──► anonymizing ──► merging ──► completed
     │                │                │                  │             │
     └────────────────┴────────────────┴──────────────────┴─────────────┴──► failed（不可恢复）
```

**流水线单元说明**：

- **预检**是任务级（对整文件做一次），产出纠偏文件后，把任务展开为 N 个分段进入 `segmenting` 的输出。
- **分段、静态、匿名化**以**分段**为流水线单元；**合并**以任务为单元（需等待该任务所有分段进入 `anonymized`/`static_dropped` 终态后触发）。
- 合并阶段需要任务级屏障：分段的匿名化完成数 + 静态丢弃数 == `segment_count - corrupt_count` 时，将任务投入合并队列。

### 1.3 队列定义

| 队列 | 元素 | 容量默认 | 生产者 | 消费者 |
|---|---|---|---|---|
| `q_precheck` | `task_id` | `QUEUE_CAPACITY`=16 | Scheduler 主循环 | 预检 worker（固定 1） |
| `q_segment` | `(task_id, fixed_path)` | 16 | 预检 worker | 分段 worker |
| `q_static` | `segment_id` | 32 | 分段 worker | 静态 worker |
| `q_anonymize` | `segment_id` | 16 | 静态 worker | 匿名化 worker |
| `q_merge` | `task_id` | 8 | 匿名化 worker（屏障满足时） | 合并 worker |
| `q_cleanup` | `(task_id, paths[])` | 64 | 各阶段 / 终态 | 清理 worker |

**背压**：所有队列为有界 `queue.Queue`，`put()` 阻塞即背压；不在 worker 内部做无限缓冲。

---

## 2. 目录与路径约定

### 2.1 路径根（环境变量注入）

| 变量 | 默认（本地开发） | 容器内 | 说明 |
|---|---|---|---|
| `INPUT_PATH` | `./input` | `/data/input` | 源文件目录，**只读** |
| `OUTPUT_PATH` | `./output` | `/data/output` | 最终输出目录 |
| `WORK_DIR` | `./workdir` | `/data/workdir` | 中间文件根目录 |
| `DB_PATH` | `./db/tasks.sqlite` | `/data/db/tasks.sqlite` | SQLite 文件 |
| `MODELS_DIR` | `./models` | `/opt/video-privacy-blur/models` | YOLO 权重目录 |

启动时校验：三个目录必须存在且可写（`INPUT_PATH` 仅需可读）；`DB_PATH` 父目录可写；否则启动失败并输出明确错误。

### 2.2 工作子目录结构

```
workdir/{task_id}/
├── fixed/            # 预检纠偏产物（原文件或纠偏后文件）
├── seg/              # 分段文件 part_%04d.mp4
├── static/           # 静态检测输出 part_%04d_dt.mp4（判定后即删）
├── anon/             # 匿名化输出 part_%04d_anon.mp4
├── merge/            # concat_list.txt、合并临时文件
└── logs/             # 每个子进程的 stderr 原始日志（按 stage 分文件，滚动保留）
```

### 2.3 输出命名

- 最终输出：`OUTPUT_PATH/{source_stem}_anonymized.mp4`
- 同名冲突：追加 `_{YYYYmmddHHMMSS}` 时间戳后缀，不覆盖已有文件。
- 源文件扩展名可能不是 mp4（如 `.mkv`），输出统一为 `.mp4`（内部已纠偏为 MP4 兼容编码）。

---

## 3. 配置规格

### 3.1 环境变量（启动期，不可热改）

| 变量 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `CPU_HIGH` / `CPU_LOW` | int | 85 / 60 | 百分比水位，触发降/增 worker |
| `MEM_HIGH` / `MEM_LOW` | int | 85 / 70 | 同上 |
| `DISK_LOW_GB` / `DISK_CRITICAL_GB` | int | 5 / 1 | 磁盘水位 |
| `QUEUE_CAPACITY` | int | 16 | 各队列默认容量 |
| `RESOURCE_SAMPLE_SEC` | int | 5 | 资源采样周期 |
| `DISK_SAMPLE_SEC` | int | 30 | 磁盘采样周期 |
| `KEEP_FAILED_INTERMEDIATE` | bool | false | 失败任务保留中间文件 |
| `SUBPROCESS_SEGMENT_TIMEOUT` | int | 600 | 单分段处理超时（秒） |
| `SUBPROCESS_ANON_TIMEOUT` | int | 1800 | 单分段匿名化超时 |
| `SUBPROCESS_MERGE_TIMEOUT` | int | 1800 | 合并超时 |
| `STDERR_TAIL_CHARS` | int | 500 | stderr 入错误记录的截取长度 |
| `ANON_CLI_PATH` | str | `video-privacy-blur` | 匿名化 CLI 可执行名/路径 |
| `WORKER_SEGMENT` / `WORKER_STATIC` / `WORKER_ANON` / `WORKER_MERGE` | int | 1 / 2 / 1 / 1 | 各阶段 worker 上限 |
| `LOG_LEVEL` | str | INFO | 日志级别 |

### 3.2 前端设置（运行期，落 `settings` 存储；任务创建时快照）

| 键 | 类型 | 范围 | 默认 | 说明 |
|---|---|---|---|---|
| `segment_enabled` | bool | — | true | 是否分段 |
| `segment_minutes` | int | 1–120 | 10 | 分段时长 |
| `static_drop_enabled` | bool | — | true | 是否静态丢弃 |
| `static_keep_ratio_threshold` | float | 0.01–0.5 | 0.05 | 低于该比例判定为静态 |
| `anonymize_faces` | bool | — | true | 人脸匿名化 |
| `anonymize_plates` | bool | — | true | 车牌匿名化 |
| `merge_enabled` | bool | — | false | 是否合并 |
| `anon_method` | enum | `pixelate` / `gaussian` | **`pixelate`** | 匿名化方法 |
| `device` | enum | `auto` / `cpu` / `cuda` | `auto` | 推理设备 |
| `anon_imgsz` | int | 320–1920 | 960 | 高级参数 |
| `anon_conf` | float | 0.05–0.95 | 0.35 | 高级参数 |
| `anon_scale` | float | 1.0–2.0 | 1.35 | 高级参数 |
| `blur_strength` | int | 以 CLI 为准 | 工具默认 | 高级参数 |
| `pixelate_blocks` | int | 以 CLI 为准 | 工具默认 | 高级参数 |
| `mpdecimate_hi` / `lo` / `frac` | int/int/float | FFmpeg 允许范围 | `64*12` / `64*5` / `0.33` | 高级参数 |

**快照规则**：任务入队时把上述设置 JSON 序列化写入 `tasks.settings_snapshot`；处理全程读取快照，**不读取实时设置**。设置变更仅影响此后新建任务。

---

## 4. 数据库规格

### 4.1 连接与并发

- 开启 WAL：`PRAGMA journal_mode=WAL;`
- 设置 `PRAGMA synchronous=NORMAL;`、`PRAGMA busy_timeout=5000;`、`PRAGMA foreign_keys=ON;`
- 写入集中在少数 worker 线程；读取（Web 查询）使用独立连接。SQLAlchemy `create_engine(..., connect_args={"check_same_thread": False})`，写操作用短事务，避免长事务阻塞 WAL checkpoint。

### 4.2 DDL（实现落地版）

> 说明：以下在 PRD 5.x 字段基础上补充了实现必需字段（`updated_at`、帧数、命令留痕等），不改变 PRD 语义。所有时间字段为 ISO 8601 UTC 字符串。

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    source_name         TEXT    NOT NULL,
    source_size         INTEGER NOT NULL,
    status              TEXT    NOT NULL,          -- queued|prechecking|segmenting|static_checking|anonymizing|merging|completed|failed
    settings_snapshot   TEXT    NOT NULL,          -- JSON
    precheck_info       TEXT,                      -- JSON：ffprobe 摘要 + 纠偏动作
    segment_count       INTEGER NOT NULL DEFAULT 0,
    static_dropped_count INTEGER NOT NULL DEFAULT 0,
    anonymized_count    INTEGER NOT NULL DEFAULT 0,
    corrupt_count       INTEGER NOT NULL DEFAULT 0,
    output_path         TEXT,
    error_message       TEXT,
    duration_seconds    REAL,
    processing_seconds  REAL,
    created_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    updated_at          TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_source_pending
    ON tasks(source_path) WHERE status NOT IN ('completed','failed');
CREATE INDEX IF NOT EXISTS ix_tasks_status_created ON tasks(status, created_at);
CREATE INDEX IF NOT EXISTS ix_tasks_created ON tasks(created_at);

CREATE TABLE IF NOT EXISTS segments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id             INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    part_index          INTEGER NOT NULL,
    part_path           TEXT    NOT NULL,          -- workdir 内路径
    status              TEXT    NOT NULL,          -- segmented|static_dropped|anonymized|anonymize_failed|corrupt|merged
    input_frames        INTEGER,
    output_frames       INTEGER,
    static_keep_ratio   REAL,
    anonymize_duration  REAL,
    anonymize_output_path TEXT,
    error_message       TEXT,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    UNIQUE(task_id, part_index)
);
CREATE INDEX IF NOT EXISTS ix_segments_task_status ON segments(task_id, status);

CREATE TABLE IF NOT EXISTS error_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    segment_id  INTEGER REFERENCES segments(id) ON DELETE SET NULL,
    stage       TEXT    NOT NULL,                  -- precheck|segment|static|anonymize|merge|cleanup
    error_type  TEXT    NOT NULL,                  -- 见 §12
    exit_code   INTEGER,
    command     TEXT,                              -- 执行的命令（脱敏后）
    message     TEXT    NOT NULL,                  -- stderr 尾部摘要
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_errors_task ON error_logs(task_id, created_at);
CREATE INDEX IF NOT EXISTS ix_errors_stage_type ON error_logs(stage, error_type, created_at);

CREATE TABLE IF NOT EXISTS stats_daily (
    date                        TEXT PRIMARY KEY,  -- YYYY-MM-DD (本地时区)
    files_total                 INTEGER NOT NULL DEFAULT 0,
    files_completed             INTEGER NOT NULL DEFAULT 0,
    files_failed                INTEGER NOT NULL DEFAULT 0,
    bytes_input                 INTEGER NOT NULL DEFAULT 0,
    bytes_output                INTEGER NOT NULL DEFAULT 0,
    segments_total              INTEGER NOT NULL DEFAULT 0,
    segments_static_dropped     INTEGER NOT NULL DEFAULT 0,
    segments_anonymized         INTEGER NOT NULL DEFAULT 0,
    total_processing_seconds    REAL    NOT NULL DEFAULT 0,
    avg_processing_seconds      REAL    NOT NULL DEFAULT 0,
    anon_method_pixelate_count  INTEGER NOT NULL DEFAULT 0,
    anon_method_gaussian_count  INTEGER NOT NULL DEFAULT 0,
    anon_seconds_pixelate       REAL    NOT NULL DEFAULT 0,
    anon_seconds_gaussian       REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,                     -- JSON 编码
    updated_at  TEXT NOT NULL
);
```

### 4.3 唯一约束与去重

`ux_tasks_source_pending` 保证同一源文件在未终态时不会被重复入队（文件被覆盖/重传时不会产生并发处理）。已终态的同一路径文件再次出现时允许新建任务。

### 4.4 统计聚合策略

- 任务进入 `completed`/`failed` 时，在**同一事务**内更新 `stats_daily` 对应日期行（upsert），并重算 `avg_processing_seconds`。
- 匿名化方法维度统计在任务终态时按 `settings_snapshot.anon_method` 累加。
- 统计查询优先读 `stats_daily`；需要更细维度（如按阶段耗时分布）时回查 `tasks`/`segments`，依赖 §4.2 索引。
- 支持"重建统计"管理操作：按 `tasks.finished_at` 全量重算 `stats_daily`（用于修复或口径变更）。

---

## 5. 状态机规格

### 5.1 任务状态转移表

| 当前 | 事件 | 下一状态 | 触发者 |
|---|---|---|---|
| （新建） | 就绪检查通过 | `queued` | InputWatcher |
| `queued` | 取出并开始预检 | `prechecking` | Scheduler |
| `prechecking` | 预检成功（含纠偏） | `segmenting` | 预检 worker |
| `prechecking` | ffprobe 失败/不可读 | `failed` | 预检 worker |
| `segmenting` | 分段完成，>=1 个有效分段 | `static_checking` | 分段 worker |
| `segmenting` | 分段失败且无法兜底 / 损坏率>50% / 无有效分段 | `failed` | 分段 worker |
| `static_checking` | 该任务所有分段进入静态终态 | `anonymizing` | 任务屏障 |
| `anonymizing` | 该任务所有分段进入匿名化终态（含失败段） | `merging` 或 `completed` | 任务屏障 |
| `merging` | 合并成功 | `completed` | 合并 worker |
| `merging` | 合并失败（含回退失败） | `failed` | 合并 worker |
| 任意非终态 | 容器重启恢复 | 从当前状态续跑 | 恢复逻辑（§14） |

**合并分支规则**：若 `merge_enabled=false`，匿名化完成且无可保留分段时直接进入 `completed`（不经过 `merging`）；若存在匿名化失败分段但仍有可保留分段，且未开合并，则任务 `completed` 但 `error_message` 标注部分分段失败（或按配置项 `TREAT_PARTIAL_AS_FAILED` 判定，默认 false）。

### 5.2 分段状态转移表

| 当前 | 事件 | 下一状态 |
|---|---|---|
| （创建） | 分段产出并通过校验 | `segmented` |
| （创建） | 分段 0 字节 / ffprobe 不可读 | `corrupt` |
| `segmented` | 静态判定为丢弃 | `static_dropped` |
| `segmented` | 静态判定为保留 / 静态阶段异常保守保留 | 进入匿名化（保持 `segmented` 直至匿名化结果写入） |
| `segmented` | 匿名化成功 | `anonymized` |
| `segmented` | 匿名化非零退出/超时 | `anonymize_failed` |
| `anonymized` | 合并成功（或未合并时作为最终输出） | `merged` |

### 5.3 任务完成度与进度

- 进度百分比（前端展示）：`progress = (static_dropped_count + anonymized_count + corrupt_count) / max(segment_count, 1) * 100`，合并阶段固定显示 99，终态 100。
- 任务屏障判定在 `segments` 写入的同一事务后，由调度器基于计数进行，避免轮询全表。

---

## 6. 阶段技术规格

### 6.1 目录监听与就绪检查

- `watchdog.observers.Observer` 监听 `INPUT_PATH` 的 `on_created` / `on_moved`。
- 扩展名过滤（大小写不敏感）：`.mp4 .mov .mkv .avi .m4v .ts`。
- **就绪检查**：事件触发后放入待定表，就绪检查线程每 `READY_CHECK_SEC`（默认 3s）探测 `st_size`，**连续两次相同且 > 0** 且文件可打开（`open(..., 'rb')` 读取 1 字节）才判定就绪。两次探测间隔 `READY_STABLE_SEC`（默认 5s）。最多等待 `READY_TIMEOUT_SEC`（默认 600s），超时记 `error_logs(stage=precheck, error_type=file_not_ready)` 并丢弃该待定项（不建任务）。
- 就绪后按 §4.3 唯一约束插入 `tasks(status='queued')`；插入冲突（已存在未终态同源任务）则跳过并记 warning。

### 6.2 预检（`precheck`）

**探测命令**：

```bash
ffprobe -v error -print_format json \
  -show_entries format=duration,format_name:stream=index,codec_type,codec_name,width,height,pix_fmt,sample_rate,channels \
  {source_path}
```

**解析与判定**：

| 流 | 兼容条件（MP4 可封装） | 不兼容动作 |
|---|---|---|
| 视频 | `codec_name ∈ {h264, hevc, av1, mpeg4}` 且 `pix_fmt ∈ {yuv420p, yuvj420p}` | 视频重编码：`libx264 -pix_fmt yuv420p` |
| 音频 | `codec_name ∈ {aac, mp3}` | 音频转码：`aac -ar 44100 -ac 2` |
| 无音频流 | — | 无需处理 |
| 无视频流 | — | 任务 `failed`（`error_type=no_video_stream`） |

**纠偏命令模板**：

```bash
# 视频不兼容（视频+音频重编码）
ffmpeg -y -nostdin -i {source} \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -c:a aac -ar 44100 -ac 2 \
  {workdir}/fixed/fixed.mp4

# 仅音频不兼容（视频流拷贝）
ffmpeg -y -nostdin -i {source} \
  -c:v copy -c:a aac -ar 44100 -ac 2 \
  {workdir}/fixed/fixed.mp4

# 完全兼容：直接软链/引用源文件，不生成 fixed 文件
```

**`precheck_info` JSON 结构**：

```json
{
  "probed_at": "2026-10-21T08:00:00Z",
  "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
  "duration": 603.2,
  "video": {"codec": "h264", "width": 1920, "height": 1080, "pix_fmt": "yuv420p"},
  "audio": {"codec": "amr_nb", "sample_rate": 8000, "channels": 1},
  "compatible": false,
  "action": "audio_transcode",
  "fixed_path": "/data/workdir/12/fixed/fixed.mp4"
}
```

`action ∈ {none, audio_transcode, full_transcode}`。纠偏命令执行失败 → `error_logs(stage=precheck, error_type=transcode_failed)`，任务 `failed`。

### 6.3 分段（`segment`）

**参数**：`SEG_SEC = segment_minutes * 60`。

**首选用流拷贝**：

```bash
ffmpeg -y -nostdin -i {fixed} \
  -c copy -f segment -segment_time {SEG_SEC} -reset_timestamps 1 \
  -segment_format mp4 {workdir}/seg/part_%04d.mp4
```

**回退（重编码）**：当流拷贝退出码非 0，或产出分段中存在 0 字节/不可读项且损坏率 > 50% 时触发：

```bash
ffmpeg -y -nostdin -i {fixed} \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -c:a aac -ar 44100 -ac 2 \
  -force_key_frames "expr:gte(t,n_forced*{SEG_SEC})" \
  -f segment -segment_time {SEG_SEC} -reset_timestamps 1 -segment_format mp4 \
  {workdir}/seg/part_%04d.mp4
```

**分段校验**：对每个 `part_*.mp4` 执行：

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,duration -of json {part}
```

判定 `corrupt` 的条件：文件大小 0、ffprobe 非零退出、无视频流、`duration<=0`。

**损坏率规则**：`corrupt_count / total_parts > 0.5` → 任务 `failed`（`error_type=segment_corrupt_ratio`）；否则损坏分段标 `corrupt` 跳过，有效分段继续。

**不分段模式**：`segment_enabled=false` 时，直接把 `fixed` 文件作为一个分段（`part_index=0`）写入 `segments`，跳过 segment muxer。

### 6.4 静态检测（`static`）

**输入帧数**：

```bash
ffprobe -v error -select_streams v:0 -count_frames \
  -show_entries stream=nb_read_frames -of csv=p=0 {part}
```

**检测命令**（与 PRD 一致）：

```bash
ffmpeg -y -nostdin -i {part} \
  -vf "mpdecimate=hi={hi}:lo={lo}:frac={frac},setpts=N/FRAME_RATE/TB" \
  -an {workdir}/static/part_{index}_dt.mp4
```

**输出帧数**：同 ffprobe `-count_frames` 读取 `_dt.mp4`。

**判定**：

```
ratio = output_frames / input_frames          # input_frames > 0
静态丢弃  <=>  ratio < static_keep_ratio_threshold
```

**异常策略（必须实现）**：

- `input_frames == 0` 或无法解析 → 保守保留，`error_type=static_probe_failed` 记 warning。
- FFmpeg 非零退出/超时 → 保守保留，`error_type=static_ffmpeg_failed`。
- 保留帧数为 0 且输入帧数 > 0 → 判为静态丢弃（ratio=0）。

无论判定结果，`_dt.mp4` 在判定后立即进入清理队列。

### 6.5 匿名化（`anonymize`）

**命令模板**（参数名以 `video-privacy-blur --help` 为准，以下为映射约定）：

```bash
{ANON_CLI_PATH} \
  --input {part} \
  --output {workdir}/anon/part_{index}_anon.mp4 \
  --method {pixelate|gaussian} \
  --device {auto|cpu|cuda} \
  [--blur-faces | --no-blur-faces] \
  [--blur-plates | --no-blur-plates] \
  --face-detector yolo \
  --face-yolo-weights {MODELS_DIR}/yolov8n-face.pt \
  --plate-weights {MODELS_DIR}/license_plate_detector.pt \
  --imgsz {imgsz} --conf {conf} --scale {scale} \
  [--blur-strength {n}] [--pixelate-blocks {n}]
```

**退出码与结果**：

| 情况 | 处理 |
|---|---|
| 退出码 0 且输出存在且 ffprobe 可读 | 分段 → `anonymized`，写入 `anonymize_output_path`、`anonymize_duration` |
| 退出码非 0 | 分段 → `anonymize_failed`，`error_logs(stage=anonymize, error_type=anonymize_nonzero)`，message=stderr 尾 500 字符 |
| 超时被 kill | `anonymize_failed`，`error_type=anonymize_timeout` |
| 退出码 0 但输出缺失/不可读 | `anonymize_failed`，`error_type=anonymize_output_invalid` |

**全失败判定**：任务所有参与匿名化的分段均为 `anonymize_failed` → 任务 `failed`；部分失败时任务继续，终态 `error_message` 记录失败段数。

**设备探测**：`device=auto` 时首次调用前执行一次探测（如 `nvidia-smi` 或 torch 可用性），探测结果缓存进程级；失败则降级 `cpu` 并记 warning，不阻塞任务。

### 6.6 合并（`merge`）

**前置**：任务屏障满足且 `merge_enabled=true`。

**一致性校验**：对所有 `anonymized` 分段执行 ffprobe，比较 `codec_name/width/height/pix_fmt/sample_rate/channels`。全部一致 → 走 A 路径，否则走 B 路径。

**A 路径（流拷贝）**：

```
# merge/concat_list.txt
file '/data/workdir/12/anon/part_0000_anon.mp4'
file '/data/workdir/12/anon/part_0001_anon.mp4'
```

```bash
ffmpeg -y -nostdin -f concat -safe 0 -i {workdir}/merge/concat_list.txt \
  -c copy {tmp_output}.mp4
```

**B 路径（concat filter 重编码）**：

```bash
ffmpeg -y -nostdin -i p0 -i p1 ... -i pN \
  -filter_complex "[0:v][0:a][1:v][1:a]...concat=n={N}:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -c:a aac -ar 44100 -ac 2 {tmp_output}.mp4
```

**输出校验与落盘**：合并产物先写 `workdir/merge/final_tmp.mp4`，通过 ffprobe 校验（有视频流、duration>0、可解码首帧）后原子 `os.replace` 到 `OUTPUT_PATH/{name}_anonymized.mp4`。校验失败 → 降级路径 → 仍失败 → 任务 `failed`（`error_type=merge_failed`）。

**注意**：B 路径要求每个分段都有音频流；若部分分段无音频，需在 filter 中补齐静音轨（`anullsrc`）或统一去音频（按 `merge_audio_policy` 配置，默认 `pad_silence`）。该策略在实现中必须显式处理，避免 concat filter 失败。

### 6.7 清理（`cleanup`）

**安全边界（强制）**：

```python
def safe_remove(path: Path, work_root: Path):
    rp = path.resolve(strict=False)
    root = work_root.resolve(strict=True)
    if root not in rp.parents and rp != root:
        raise SecurityError(f"refuse to delete outside workdir: {rp}")
    if path.is_symlink():          # 拒绝清理符号链接本身以外的目标
        path.unlink(missing_ok=True); return
    if rp.is_dir(): shutil.rmtree(rp, ignore_errors=True)
    else: rp.unlink(missing_ok=True)
```

- 删除前断言目标位于 `WORK_DIR/{task_id}/` 之下；**禁止**对 `INPUT_PATH`、`OUTPUT_PATH` 执行任何删除/移动。
- 幂等：文件不存在视为成功。
- 失败（权限/占用）→ `error_logs(stage=cleanup, error_type=cleanup_failed)` + `cleanup_warnings`，不阻塞终态。

**清理触发点**：见 PRD 3.7 表格。实现上统一投递到 `q_cleanup`，由清理 worker 串行执行，避免并发删除同一目录。

**终态兜底**：任务进入终态事务提交后，投递 `(task_id, workdir)` 到清理队列；`KEEP_FAILED_INTERMEDIATE=true` 且任务失败时跳过兜底清理，并在 `error_logs.message` 追加保留路径。

---

## 7. 调度与并发规格

### 7.1 资源水位控制

`ResourceMonitor` 每 `RESOURCE_SAMPLE_SEC` 采样 `psutil.cpu_percent(interval=None)` 与 `psutil.virtual_memory().percent`，维护状态机：

```
NORMAL --(cpu>CPU_HIGH or mem>MEM_HIGH)--> THROTTLED
THROTTLED --(cpu<CPU_LOW and mem<MEM_LOW)--> NORMAL
```

- 进入 `THROTTLED`：向 Scheduler 发信号，将各阶段"期望 worker 数"下调 1（不低于 1）；worker 处理完当前元素后检查信号并退出。
- 进入 `NORMAL`：每 `RESOURCE_SAMPLE_SEC` 增加 1，直至上限（§3.1 `WORKER_*`）。
- 分段与清理 worker 不参与下调（避免断流与磁盘堆积）。

### 7.2 任务级并发上限

`MAX_ACTIVE_TASKS`（默认 4）：同时处于非终态的任务数上限；达到后 Scheduler 主循环不再从 `queued` 取任务。该参数防止大量任务同时展开分段导致磁盘爆量。

### 7.3 失败重试

- 不自动重试不可恢复失败（`transcode_failed`、`no_video_stream`、`segment_corrupt_ratio`、`merge_failed`）。
- 超时类失败（`*_timeout`）允许 1 次重试（任务级 `retry_count`，默认上限 1），重试从失败阶段开始，已完成的子单元复用。

---

## 8. 子进程管理规格

所有 FFmpeg / CLI 调用经 `app/media/ffmpeg_utils.py` 统一封装：

- 参数以 `list[str]` 传入，**不使用 `shell=True`**。
- 捕获 stdout/stderr 到 `workdir/{task_id}/logs/{stage}_{unit}.log`，同时内存保留尾部 `STDERR_TAIL_CHARS`。
- 超时：`Popen.wait(timeout=...)` → `terminate()` → 等 10s → `kill()`；超时标记对应错误类型。
- 退出码与命令（脱敏：绝对路径替换为相对占位）写入 `error_logs.command`。
- 返回统一结构：`RunResult(returncode, stdout_tail, stderr_tail, duration, timed_out, log_path)`。

---

## 9. 磁盘水位保护

`DiskGuard` 每 `DISK_SAMPLE_SEC` 检查 `WORK_DIR` 所在分区：

- `free < DISK_LOW_GB`：置"暂停取新任务"标志，Scheduler 主循环暂停从 `queued` 取任务（在途任务继续）。
- `free < DISK_CRITICAL_GB`：额外对所有已完成任务的 `workdir` 子目录投递强制清理，并记录 `error_logs(stage=cleanup, error_type=disk_critical)`（任务级告警，不阻断）。
- 恢复：`free > DISK_LOW_GB + 1GB`（滞回）时解除暂停。

---

## 10. REST API 规格

统一前缀 `/api`，JSON 请求/响应，错误响应 `{"error": {"code": "...", "message": "..."}}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{"status":"ok","version":"...","db":"ok"}` |
| GET | `/api/config` | 返回 `input_path`、`output_path`、当前设置、只读环境信息 |
| PUT | `/api/settings` | 更新前端设置（校验范围），返回新设置 |
| GET | `/api/tasks?status=&page=&page_size=` | 任务分页列表，按 `created_at desc`；`status` 可多值逗号分隔 |
| GET | `/api/tasks/{id}` | 任务详情（含 `precheck_info`、计数、耗时） |
| GET | `/api/tasks/{id}/segments` | 分段列表（分页） |
| POST | `/api/tasks/{id}/retry` | 仅对允许重试的失败任务生效 |
| GET | `/api/errors?stage=&error_type=&task_id=&page=` | 报错分页 |
| GET | `/api/errors/{id}` | 报错详情（完整 message、command、exit_code） |
| GET | `/api/stats/summary?range=day|week|month` | 聚合指标（§PRD 8.1–8.3） |
| GET | `/api/stats/timeseries?metric=&from=&to=` | 时间序列（文件数/耗时/失败数/匿名化方法分布） |
| POST | `/api/stats/rebuild` | 重建 `stats_daily`（管理操作，可加鉴权） |

**分页**：`page` 从 1 开始，`page_size` 默认 20、上限 200；响应含 `total`。

**校验**：`PUT /api/settings` 对每个字段做类型与范围校验，越界返回 `400 settings_invalid`。

---

## 11. WebSocket 协议

端点：`/ws`。服务端单向推送，客户端可发送 `{"type":"ping"}`，服务端回 `{"type":"pong"}`（30s 心跳）。

**事件**：

```json
{"type":"task_updated","data":{
  "task_id":12,"status":"anonymizing",
  "segment_count":6,"static_dropped_count":2,"anonymized_count":3,
  "corrupt_count":0,"progress":83.3,
  "error_message":null,"updated_at":"2026-10-21T08:10:00Z"}}

{"type":"segment_updated","data":{
  "task_id":12,"segment_id":45,"part_index":3,
  "status":"anonymized","static_keep_ratio":0.42,
  "anonymize_duration":87.5,"updated_at":"2026-10-21T08:09:30Z"}}

{"type":"stats_updated","data":{"date":"2026-10-21"}}
```

**规则**：

- 事件在 DB 事务提交后发布，保证"看到消息即可读到数据"。
- 前端断线重连后先 `GET /api/tasks?...` 拉快照，再接续 WS 增量。
- 单连接推送限速：每类事件合并节流 `WS_THROTTLE_MS`（默认 200ms），避免高频刷新压垮前端。

---

## 12. 错误码与错误类型枚举

### 12.1 `error_logs.error_type`（闭集，新增需评审）

| 值 | stage | 是否任务级失败 | 说明 |
|---|---|---|---|
| `file_not_ready` | precheck | 否（不建任务） | 就绪检查超时 |
| `probe_failed` | precheck | 是 | ffprobe 非零/不可读 |
| `no_video_stream` | precheck | 是 | 无视频流 |
| `transcode_failed` | precheck | 是 | 纠偏命令失败 |
| `segment_failed` | segment | 是 | 分段命令失败（含回退后） |
| `segment_corrupt_ratio` | segment | 是 | 损坏率 > 50% |
| `segment_part_corrupt` | segment | 否 | 单分段损坏（已跳过） |
| `static_probe_failed` | static | 否 | 帧数获取失败，保守保留 |
| `static_ffmpeg_failed` | static | 否 | 静态检测命令失败，保守保留 |
| `anonymize_nonzero` | anonymize | 条件性 | 单段失败；全失败则任务失败 |
| `anonymize_timeout` | anonymize | 条件性 | 超时 |
| `anonymize_output_invalid` | anonymize | 条件性 | 输出缺失/不可读 |
| `merge_failed` | merge | 是 | 两路径均失败 |
| `merge_output_invalid` | merge | 是 | 合并产物校验失败 |
| `cleanup_failed` | cleanup | 否 | 清理失败 |
| `disk_critical` | cleanup | 否 | 磁盘告警 |
| `device_fallback` | anonymize | 否 | GPU 不可用降级 CPU |

### 12.2 API 错误码

| code | HTTP | 说明 |
|---|---|---|
| `not_found` | 404 | 资源不存在 |
| `settings_invalid` | 400 | 设置越界/类型错误 |
| `invalid_state` | 409 | 对非允许状态的任务执行操作（如重试不可重试任务） |
| `internal_error` | 500 | 未预期异常（记录日志） |

---

## 13. 日志规范

- 应用日志按行输出到 stdout，格式：`{ts} {level} {module} task={task_id} segment={segment_id} stage={stage} event={event} msg=...`。
- 子进程原始日志落 `workdir/{task_id}/logs/`，任务终态清理时随之删除（排障需在失败时及时导出；`KEEP_FAILED_INTERMEDIATE=true` 可保留）。
- 关键事件必须记录：任务创建、阶段进入/退出、纠偏动作、分段校验失败、静态判定结果（ratio）、匿名化耗时、合并路径选择（A/B）、清理结果、恢复动作。

---

## 14. 重启恢复规格

启动时：

1. 执行 DDL（幂等）与 `PRAGMA` 设置。
2. 扫描 `tasks.status NOT IN ('completed','failed')`：
   - `queued`：重新入 `q_precheck`。
   - `prechecking`：检查 `workdir/fixed` 是否已有有效 `fixed.mp4`；有则跳到 `segmenting`，否则重跑预检。
   - `segmenting`：检查 `seg/` 已有分段并逐个校验，缺失/损坏的按原分段策略重建（**不依赖旧中间文件**，可整体重跑分段）。
   - `static_checking`/`anonymizing`：按 `segments.status` 逐段决定复用或重跑；处于 `segmented` 的分段重新进入对应阶段；`merged`/`anonymized`/`static_dropped` 复用。
   - `merging`：若 `merge/` 有校验通过的临时产物可完成落盘，否则重跑合并。
3. 恢复过程中任何"中间文件缺失"都必须从对应阶段重新生成，**不允许因中间文件缺失而失败**。
4. 恢复动作写 `error_logs(stage=..., error_type=...)` 之外的应用日志（不污染业务错误统计）。

---

## 15. 安全边界（实现必须遵守）

1. **源文件只读**：仅以只读方式打开源文件；不得 `rename`/`unlink`/`chmod` `INPUT_PATH` 下任何文件。
2. **输出不覆盖**：`OUTPUT_PATH` 命名冲突时追加时间戳，禁止覆盖既有文件。
3. **清理边界**：§6.7 `safe_remove` 实现为唯一删除入口；代码评审强制检查。
4. **命令注入**：子进程一律 `list` 传参；输出路径由 `task_id` 与固定模板生成，不接受外部输入拼接。
5. **符号链接**：遍历/校验 `workdir` 时使用 `resolve()`，拒绝逃逸到根外的路径。
6. **路径暴露**：API 返回路径时可选脱敏（`PATH_MASK=true` 时仅返回相对 `INPUT_PATH`/`OUTPUT_PATH` 的名称）。

---

## 16. 测试规格

### 16.1 Fixtures（`tests/fixtures/`）

| 文件 | 用途 |
|---|---|
| `normal_h264_aac.mp4` | 正常路径（分段/静态/匿名化/合并全走） |
| `audio_amr.mp4` | 音频不兼容，触发 `audio_transcode` 纠偏 |
| `video_mpeg4_incompatible.avi` | 视频不兼容，触发 `full_transcode` |
| `no_audio.mp4` | 无音频流，验证合并 B 路径静音补齐策略 |
| `corrupt_truncated.mp4` | 截断文件，验证 `probe_failed` |
| `static_short.mp4` | 静态画面，验证 `ratio≈0` 丢弃 |
| `mixed_dynamic_static.mp4` | 前半动态后半静态，验证"整段不裁剪"规则 |
| `tiny_1s.mp4` | 极短时长边界（分段数为 1） |
| `big_60min.mp4`（生成脚本产出） | 长时长与多分段、磁盘与并发验证 |

### 16.2 单元测试点

- 预检：兼容性矩阵判定、`precheck_info` 结构、纠偏命令拼装、转码失败处理。
- 分段：流拷贝失败触发回退、分段校验与损坏率阈值（0/49%/51% 边界）、不分段模式单段。
- 静态：帧数获取失败保守保留、ratio 边界（=0、=阈值、>阈值）、`_dt` 文件清理。
- 匿名化：退出码 0/非0、超时、输出不可读、全失败与部分失败的任务级判定。
- 合并：一致性校验、A/B 路径选择、B 路径无音频补齐、输出校验失败降级、最终文件名冲突。
- 清理：边界逃逸拒绝（`../`、绝对路径、符号链接）、幂等、失败不阻塞终态。
- 状态机：非法转移拒绝、任务屏障计数、重试仅对超时类生效。

### 16.3 集成测试点

- 端到端：投文件 → 终态 `completed` → 输出可播放 → `workdir` 仅剩空目录/已清理 → 源文件 mtime/size 不变。
- 失败注入：kill 匿名化进程 → 任务 `failed`/部分失败语义正确 → 错误记录可查 → 中间文件按配置清理。
- 重启恢复：在 `segmenting`/`anonymizing` 中间态 kill 容器 → 重启后继续直至终态，中间文件缺失不导致失败。
- 并发/资源：`MAX_ACTIVE_TASKS` 生效；资源高水位 worker 减少；背压使队列不无限增长。
- 磁盘保护：模拟低水位暂停取任务；临界水位触发已完成任务清理。

### 16.4 验收口径（对应 PRD 第 10 节）

- 预检纠偏覆盖率 100%（fixtures 全部经过 ffprobe 判定）。
- 编码不兼容硬失败率 < 5%（以 fixtures 全绿为准）。
- 统计查询在 10 万任务规模下 < 1s（种子数据脚本 `tests/seed/stats_seed.py`）。
- `workdir` 终态清理率 100%（除保留开关开启场景）。

---

## 17. 性能基线（纯 CPU，4C/8G）

- 10 分钟 1080p H.264/AAC 视频，默认设置（分段 10 分钟、静态开、马赛克、不合并）：端到端 ≤ 3× 视频时长。
- 各阶段预期占比（参考，用于回归对比）：预检 < 1%、分段 < 5%、静态 < 5%、匿名化 80–90%、合并（若开）< 5%。
- 线程配置默认下内存峰值目标 < 6 GB（含 YOLO 推理）。

---

## 18. 未决与实现前需确认

| 项 | 影响 | 处理 |
|---|---|---|
| `video-privacy-blur` CLI 真实参数名（`--input/--output/--method/--blur-faces/--no-blur-faces` 等） | 命令模板、设置映射 | 开发前执行 `--help` 并以实际输出修订 §6.5 与本 SPEC 参数表 |
| `--blur-strength` / `--pixelate-blocks` 默认值与取值范围 | 前端高级参数展示 | 以 CLI 为准，SPEC 暂标"工具默认" |
| 合并 B 路径的音频策略（补齐静音 / 去音频 / 拒绝） | 合并成功率 | 默认 `pad_silence`，作为 `merge_audio_policy` 配置项实现 |
| `TREAT_PARTIAL_AS_FAILED` 是否纳入配置 | 部分失败任务的终态语义 | 默认 false；若合规要求严格可开启 |
| GPU 探测方式（`nvidia-smi` vs torch 探测） | 启动耗时、容器兼容 | 优先 `nvidia-smi`，超时 2s 视为不可用 |

---

> 变更记录：v0.1 初版（依据 PRD v0.1 落地）。后续任何影响 PRD 业务规则、默认值、清理边界、状态机语义的改动，需先修订 PRD，再同步本文。