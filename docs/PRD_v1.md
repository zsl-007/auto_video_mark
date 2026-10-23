# 自动视频匿名化系统 PRD

> 返回 [README](../README.md) ｜ 版本：v0.1 ｜ 状态：评审中

## 1. 产品概述

### 1.1 产品定位

构建一套**目录监听驱动的批量视频隐私处理系统**。用户在 `input` 目录投放视频源文件，系统自动完成分段、静态视频丢弃、人脸/车牌匿名化、合并（可选）并输出到 `output` 目录，全程无需人工干预。系统以 Docker 方式部署，提供 Web 前端用于配置、监控和统计。

### 1.2 核心约束与设计原则

- **无人值守批处理**：文件到达即触发，流水线自动推进，失败可降级、可记录、可重试。
- **编码健壮性优先**：不信任文件扩展名，对每个输入视频做 ffprobe 预检，对音频/视频编码不兼容 MP4 的情况自动纠正，分段与合并阶段编码失败有回退路径。
- **源文件只读、中间文件即用即清**：`input` 目录源文件与 `output` 目录最终结果**永不删除、永不修改**；仅清理工作目录中的中间过程文件，磁盘占用可控。
- **资源感知的流水线并行**：各阶段之间通过有界队列解耦，根据 CPU/内存水位动态调节并行度，避免压垮主机。

## 2. 用户角色与使用场景

| 角色 | 场景 |
|---|---|
| **运维/操作员** | 将视频批量放入 `input` 目录，在前端查看进度和报错，调整分段时长、是否合并等参数 |
| **隐私合规人员** | 检查匿名化结果是否满足要求（人脸/车牌是否被充分模糊），查看统计报表 |

核心流程：**放入文件 → 自动检测 → 自动流水线处理 → 结果出现在 output → 前端查看状态**。

## 3. 功能需求

### 3.1 目录监听与任务生成

使用 `watchdog` 监听 `input` 目录的新文件创建事件。文件创建事件可能早于文件写入完成，因此**不立即触发处理**，而是将任务置为 `pending`，由一个“就绪检查器”定期确认文件大小稳定（连续两次探测大小不变）后再入队。任务写入 SQLite，状态为 `queued`。

支持的文件扩展名：`.mp4`、`.mov`、`.mkv`、`.avi`、`.m4v`、`.ts`。扩展名仅作为入口过滤，实际编码以 ffprobe 结果为准。

### 3.2 输入预检（编码兼容性纠偏）

这是健壮性的第一道防线。对每个入队文件，在分段之前执行 **`ffprobe` 预检**：

```bash
ffprobe -v error -show_entries stream=codec_type,codec_name -of csv=p=0 input.mp4
```

检查逻辑：

| 检查项 | 不兼容时处理 |
|---|---|
| 视频编码不在 MP4 可封装集合（H.264/H.265/AV1 等） | 视频流统一转码为 `libx264` + `yuv420p` |
| 音频编码不在 MP4 可封装集合（AAC/MP3 等） | 音频流转码为 `aac`，并显式指定 `-ar 44100 -ac 2` |
| 音视频流参数（采样率、声道、像素格式）缺失或不标准 | 补全标准参数后重新封装/转码 |

已知典型不兼容：`amr_nb`、`wavpack`、`dts`、`eac3`、`truehd` 等音频编码在 MP4 中可能无法写入头信息，表现为 `Could not find tag for codec ... not currently supported in container`。处理策略是**将 `-c copy` 替换为 `-c:v copy -c:a aac`（视频不重编码、仅音频转 AAC）**；若视频编码也不兼容，则 `-c:v libx264 -c:a aac`。

预检结果记录到任务的 `precheck_info` 字段（JSON），供后续阶段和统计使用。**预检纠偏产生的中间文件写入工作目录（workdir），源文件保留在 input 目录，系统对其只读、不修改。**

### 3.3 视频分段

**分段策略**：当“是否分段”开启时，按用户设置的分段时长（默认 10 分钟）切分。使用 FFmpeg `segment` muxer，优先采用 `-c copy` 尝试流拷贝分段以保留画质并提速：

```bash
ffmpeg -i input_fixed.mp4 -c copy -f segment -segment_time 600 -reset_timestamps 1 -segment_format mp4 part_%04d.mp4
```

**分段编码失败的兜底**：

- **失败场景 A：`-c copy` 分段失败**（关键帧间隔或编码参数导致 muxer 报错）。回退到重新编码分段：`-c:v libx264 -c:a aac -force_key_frames "expr:gte(t,n_forced*SEG_DURATION)"`，强制在分段点生成关键帧，保证每个分段可独立播放。
- **失败场景 B：某些分段产出 0 字节或无法被 ffprobe 读取**。该分段标记为 `segment_corrupt`，不进入后续流水线；若同一源文件超过 50% 分段损坏，整任务标记为 `failed` 并记录原因，否则对损坏分段跳过并告警。
- **分段精度说明**：`segment` muxer 配合 `-c copy` 时切分点会落在最近的关键帧上，不保证严格等于设定时长。若对时长精度有强要求，备用方案为 HLS 分段：`-f hls -hls_time N -hls_flags split_by_time+round_durations`。PRD 默认采用 segment muxer 的关键帧对齐方案，在设置项中注明“实际分段时长可能因关键帧间隔略有浮动”。

**不分段模式**：跳过分段，将预检纠偏后的文件直接作为单个“段”送入后续流水线。

### 3.4 静态视频丢弃

对每个分段（或不分段时的完整文件）执行静态检测。使用用户指定的 `mpdecimate` 方案，以**输出结果的帧数作为判定依据**：

```bash
ffmpeg -i part_0000.mp4 -vf "mpdecimate,setpts=N/FRAME_RATE/TB" -an output_dt.mp4
```

判定规则：比较输入分段与 `output_dt.mp4` 的**视频帧数**。若 `output_dt` 的帧数低于输入帧数的 `static_threshold`（默认 0.05，即保留帧不足 5%），判定为“静态视频”，该分段被丢弃，不进入匿名化阶段。阈值可配置。

`mpdecimate` 的内部阈值参数（`hi`、`lo`、`frac`）暴露为高级设置，默认使用 FFmpeg 默认值（`hi=64*12`，`lo=64*5`，`frac=0.33`）。若用户希望更激进地丢弃静态段，可提高 `frac` 或降低 `hi`。

**注意**：静态丢弃只丢弃**整个分段**，不对分段内部做帧级裁剪。若分段中只有部分静态内容，仍保留该分段，后续匿名化处理正常进行。

### 3.5 人脸匿名化与车牌匿名化

调用 `video-privacy-blur` 的 CLI 作为子进程执行。该工具使用 Ultralytics YOLO 进行检测，OpenCV 进行模糊/马赛克，支持 CPU/GPU/MPS，人脸和车牌可独立开关。

**调用参数映射**：

| 系统设置 | `video-privacy-blur` 参数 |
|---|---|
| 人脸匿名化 开 | `--blur-faces`（或 `--no-blur-faces` 关闭） |
| 车牌匿名化 开 | `--blur-plates`（或 `--no-blur-plates` 关闭） |
| 设备选择 | `--device auto`（生产环境优先 `cuda`，不可用时回退 `cpu`） |
| 匿名化方法 | `--method pixelate`（默认）或 `--method gaussian`（用户可切换） |
| 人脸检测器 | `--face-detector yolo --face-yolo-weights models/yolov8n-face.pt`（YOLO 对人脸角度变化更鲁棒） |
| 车牌模型 | `--plate-weights models/license_plate_detector.pt` |

`--imgsz`、`--conf`、`--scale` 作为高级参数暴露，默认分别为 `960`、`0.35`、`1.35`。匿名化方法的强度参数（`--blur-strength`、`--pixelate-blocks`）也作为高级设置，默认按工具推荐值。**默认匿名化方法为马赛克（pixelate）**，高斯模糊作为可选方法供用户切换。

**性能备注**：该工具中仅 YOLO 模型使用 GPU，Haar 人脸检测为纯 CPU。因此在 GPU 可用时，人脸检测器应选择 `yolo` 才能利用 GPU 加速。

**失败处理**：若 `video-privacy-blur` 在某个分段上以非零退出码结束，且该分段不是最后一个分段，标记该分段为 `anonymize_failed`，继续处理其他分段；若所有分段均失败，任务标记为 `failed`。失败原因截取 stderr 末 500 字符存入报错记录。

### 3.6 视频合并

当“是否合并”开启时，将所有成功完成匿名化的分段按原顺序合并为最终输出。合并策略取决于分段编码参数是否一致：

- **编码参数一致**（分段阶段使用了 `-c copy` 或同一套重编码参数）：使用 **concat demuxer + `-c copy`**，无损快速合并：

  ```bash
  ffmpeg -f concat -safe 0 -i concat_list.txt -c copy output_final.mp4
  ```

- **编码参数不一致**（例如部分分段经历了重编码兜底、部分为流拷贝，或分辨率/像素格式有差异）：concat demuxer 无法直接拼接不匹配的流，此时使用 **concat filter 统一重编码**为 H.264/AAC MP4。

合并失败的兜底：先尝试 `-c copy`；若 muxer 报错或输出无法播放，自动降级为 concat filter 重编码。重编码仍失败则将任务标记为 `merge_failed`，并在报错记录中提示可手动拼接。

### 3.7 中间文件清理

**清理范围原则**：系统只清理**工作目录（`workdir`）中由本系统产生的中间过程文件**。以下文件**永不删除、永不修改**：

- **源文件**：`input` 目录中的原始视频文件，无论任务成功或失败，均原样保留在 `input` 目录，系统对其只读访问。
- **最终输出文件**：`output` 目录中的最终结果视频。

中间过程文件包括：预检纠偏产生的转码/重封装临时文件、分段文件、静态检测输出文件、匿名化阶段的输入/输出临时文件、合并阶段的临时文件与 concat 列表文件等。

**清理时机与策略**：

| 中间文件类型 | 清理时机 |
|---|---|
| 预检纠偏临时文件 | 分段阶段成功读取后立即删除；若分段阶段回退重编码，则在重编码完成后删除 |
| 分段文件 | 该分段完成静态检测并成功进入匿名化阶段、且匿名化产出就绪后删除（流水线内清理） |
| 静态检测输出文件 | 判定完成后立即删除（无论判定为静态丢弃还是保留） |
| 匿名化输入/输出临时文件 | 该分段合并完成（或未开启合并时作为最终输出落盘）后删除 |
| 合并临时文件、concat 列表 | 合并完成并校验输出可读后删除 |

**任务终态清理**：

- 任务进入 `completed` 或 `failed` 终态后，执行一次该任务工作子目录的兜底清理，删除残留的所有中间过程文件。
- **兜底清理不触碰源文件和最终输出文件**。
- 若任务 `failed` 且已产生部分匿名化分段（如合并失败场景），中间分段默认清理；仅在 `KEEP_FAILED_INTERMEDIATE=true`（默认 `false`）时保留供人工排查，且保留目录在报错记录中给出路径。

**清理失败处理**：清理失败（文件被占用、权限不足等）记录到 `cleanup_warnings` 与 `error_logs`（`error_type=cleanup_failed`），不阻塞任务状态变为终态，但前端报错记录中可见，供运维手动处理。

**幂等性**：清理操作幂等，文件不存在视为清理成功；容器重启后的恢复流程不会因中间文件已删除而失败。

**安全边界**：清理器以“任务工作子目录”（`workdir/{task_id}/`）为边界执行删除，从代码层面杜绝误删 `input` 与 `output` 路径。

## 4. 技术架构

### 4.1 整体架构

采用 **FastAPI + SQLite（WAL 模式）+ 后台 Worker 进程/线程组 + watchdog 监听器** 的单体架构。FastAPI 处理 HTTP API 和 WebSocket 实时推送；后台 Worker 执行流水线；watchdog 独立线程负责目录监听。SQLite 开启 WAL 模式以支持前端读查询与后台写入并发。

```
[watchdog] → 写入 tasks (queued)
                ↓
[调度器] ← 资源监控 (psutil)
    ↓ 按阶段分发
[分段 Worker] → [静态检测 Worker] → [匿名化 Worker] → [合并 Worker]
    ↓ 各阶段间有界队列 (queue.Queue)
[清理器]（仅清理 workdir）
    ↓
[FastAPI + WebSocket] → 前端
[SQLite WAL] ← 统计查询
```

### 4.2 流水线并发模型

核心设计是**阶段间流水线 + 阶段内多 worker**。每个阶段维护一个有界输入队列，若干 worker 从队列取任务处理后放入下一阶段队列。队列长度和 worker 数量由资源监控器动态调节。

资源监控通过 `psutil` 采样 CPU 使用率和内存使用率。调节策略：

- 当 `cpu_percent > CPU_HIGH`（默认 85%）或 `memory_percent > MEM_HIGH`（默认 85%）时，**减少**除“分段”和“清理”外各阶段的活跃 worker 数，但**不停止**已启动的 worker，让其在当前任务完成后自然退出。
- 当资源回落到 `CPU_LOW`（默认 60%）和 `MEM_LOW`（默认 70%）以下时，**逐步增加** worker，上限为 `max_workers_per_stage`（默认：分段 1，静态 2，匿名化 1，合并 1）。
- 匿名化阶段是 GPU/CPU 密集型，默认 `max_workers=1`，避免多个 YOLO 推理争抢显存/算力。若部署环境为纯 CPU 且核心数充足，可调高至 2。

**阶段间背压**：当某阶段队列满时，上游 worker 阻塞在 `put()` 上，自然形成背压，防止中间文件无限膨胀。

### 4.3 任务状态机

每个源文件对应一个 `tasks` 记录，状态流转如下：

```
queued → prechecking → segmenting → static_checking 
→ anonymizing → merging → completed
                              ↘ failed (任一阶段不可恢复)
```

各阶段内部每个分段/子单元有独立的子状态，父任务状态由子状态聚合得出（全部完成→完成，任一不可恢复失败且影响最终输出→失败）。

## 5. 数据模型（SQLite）

### 5.1 `tasks` 表

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `source_path` | TEXT | input 目录中的源文件路径（只读，永不删除） |
| `source_name` | TEXT | 源文件名 |
| `source_size` | INTEGER | 字节数 |
| `status` | TEXT | queued / prechecking / segmenting / static_checking / anonymizing / merging / completed / failed |
| `settings_snapshot` | TEXT JSON | 任务创建时的设置快照（分段、时长、静态丢弃、人脸、车牌、合并、匿名化方法） |
| `precheck_info` | TEXT JSON | ffprobe 结果及纠偏动作 |
| `segment_count` | INTEGER | 分段总数（不分段时为 1） |
| `static_dropped_count` | INTEGER | 被判定为静态并丢弃的分段数 |
| `anonymized_count` | INTEGER | 成功匿名化的分段数 |
| `output_path` | TEXT | 最终输出路径 |
| `error_message` | TEXT | 失败原因摘要 |
| `created_at` / `started_at` / `finished_at` | TEXT ISO8601 | |
| `duration_seconds` | REAL | 源视频时长（ffprobe 获取） |
| `processing_seconds` | REAL | 总处理耗时 |

### 5.2 `segments` 表（子单元追踪）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `task_id` | INTEGER FK | |
| `part_index` | INTEGER | 分段序号 |
| `part_path` | TEXT | 分段文件路径（workdir 内） |
| `status` | TEXT | segmented / static_dropped / anonymized / anonymize_failed / merged / corrupt |
| `static_keep_ratio` | REAL | 静态检测后保留帧比例 |
| `anonymize_duration` | REAL | 匿名化耗时 |
| `error_message` | TEXT | 该分段的错误信息 |

### 5.3 `error_logs` 表

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `task_id` | INTEGER FK | |
| `segment_id` | INTEGER FK nullable | |
| `stage` | TEXT | precheck / segment / static / anonymize / merge / cleanup |
| `error_type` | TEXT | codec_incompatible / segment_failed / anonymize_nonzero / merge_failed / cleanup_failed |
| `message` | TEXT | 原始 stderr 摘要 |
| `created_at` | TEXT | |

### 5.4 `stats_daily` 表（预聚合统计）

| 列 | 类型 | 说明 |
|---|---|---|
| `date` | TEXT PK | YYYY-MM-DD |
| `files_total` / `files_completed` / `files_failed` | INTEGER | |
| `bytes_input` / `bytes_output` | INTEGER | |
| `segments_total` / `segments_static_dropped` / `segments_anonymized` | INTEGER | |
| `total_processing_seconds` | REAL | |
| `avg_processing_seconds` | REAL | |

## 6. 前端交互规格

### 6.1 页面结构

**主仪表盘**：展示 `input_path` 和 `output_path` 的当前值（只读展示，路径在部署时通过环境变量注入）、当前处理任务列表、最近报错摘要。

**设置面板**：

| 设置项 | 控件 | 默认值 |
|---|---|---|
| 是否分段 | 开关 | 开 |
| 分段时长（分钟） | 数字输入，1–120 | 10 |
| 是否静态丢弃 | 开关 | 开 |
| 静态判定阈值（保留帧比例） | 数字输入，0.01–0.5 | 0.05 |
| 是否人脸匿名化 | 开关 | 开 |
| 是否车牌匿名化 | 开关 | 开 |
| 是否合并视频 | 开关 | 关 |
| 匿名化方法 | 下拉：高斯模糊 / 马赛克 | **马赛克** |
| 设备 | 下拉：auto / cpu / cuda | auto |
| 高级参数（可折叠） | imgsz / conf / scale / blur-strength / pixelate-blocks / mpdecimate 阈值 | 工具默认值 |

设置变更**仅对之后入队的任务生效**，已在处理中的任务使用其 `settings_snapshot`。

**当前处理任务**：表格展示 `source_name`、状态（带颜色标识）、进度百分比、分段统计（总数/已静态丢弃/已匿名化）、已耗时。状态通过 WebSocket 实时更新，不依赖轮询。每行可展开查看分段级详情。

**报错记录**：按时间倒序展示，可按 stage 和 error_type 筛选。每条记录展示任务名、阶段、错误摘要，点击可展开完整 stderr 片段。

**统计分析**（详见第 8 节）：卡片和图表结合。

### 6.2 实时推送

使用 WebSocket 推送两类事件：`task_updated`（任务状态变更）和 `segment_updated`（分段状态变更）。前端建立单一 WebSocket 连接，按 `task_id` 过滤展示。连接断开时自动重连，重连后通过 REST 拉取当前快照。

## 7. 错误处理与健壮性

### 7.1 分层错误处理策略

| 层级 | 策略 |
|---|---|
| **预检层** | ffprobe 失败（文件损坏/不可读）→ 任务 `failed`，不进入后续阶段。编码不兼容 → 自动纠偏，记录 `precheck_info`，继续。源文件始终只读不修改。 |
| **分段层** | `-c copy` 失败 → 自动重编码兜底；仍失败 → 任务 `failed`。个别分段 0 字节/不可读 → 标记 `corrupt`，若损坏比例 ≤ 50% 继续，否则整任务失败。 |
| **静态层** | FFmpeg 非零退出 → 该分段不参与静态丢弃（视为“保留”），记录 warning，继续匿名化。判定依据缺失（无法获取帧数）→ 保守保留分段。 |
| **匿名化层** | 单个分段失败 → 标记 `anonymize_failed`，其他分段继续。全部分段失败 → 任务 `failed`。 |
| **合并层** | `-c copy` 失败 → concat filter 重编码兜底；仍失败 → 任务 `merge_failed`。 |
| **清理层** | 仅清理 `workdir` 中间过程文件，**源文件与最终输出文件永不删除、永不修改**。清理失败记录 warning，不影响任务终态。 |

### 7.2 超时与僵尸进程

每个 FFmpeg / video-privacy-blur 子进程有可配置超时（默认：分段单段 600s，匿名化单段 1800s，合并 1800s）。超时后 `terminate()`，等待 10s 后 `kill()`，该子单元标记为超时失败，按上述分层策略处理。

### 7.3 磁盘水位保护

后台线程定期检查工作目录所在分区的可用空间。当可用空间低于 `DISK_LOW_GB`（默认 5 GB）时，**暂停从 queued 队列取新任务**，已在进行中的任务继续完成。恢复后自动继续。若可用空间低于 `DISK_CRITICAL_GB`（默认 1 GB），强制清理所有已完成任务的 workdir 中间文件并记录告警（**不触碰 input 源文件与 output 最终输出**）。

## 8. 统计分析规格

统计分析基于 `tasks` 和 `segments` 表聚合，`stats_daily` 作为加速缓存。前端展示以下维度：

### 8.1 总量指标（支持按日/周/月切换）

- 累计处理文件数、成功数、失败数、失败率。
- 累计输入总时长、输出总时长、静态丢弃节省的时长。
- 累计人脸匿名化分段数、车牌匿名化分段数（按 `settings_snapshot` 中的开关分组）。
- 累计输入字节数、输出字节数、中间文件产生量（可估算，统计产生量而非保留量）。
- **匿名化方法分布**（马赛克 / 高斯模糊的任务数与处理时长对比），便于评估不同方法的效率差异。

### 8.2 效率指标

- 平均单文件处理耗时（按文件时长分桶：<1min，1–10min，10–60min，>60min）。
- 各阶段平均耗时占比（预检、分段、静态、匿名化、合并），以堆叠柱状图展示。
- 匿名化阶段吞吐：平均每秒处理帧数（从 `anonymize_duration` 和视频帧率估算）。
- **按匿名化方法（马赛克/高斯模糊）分组的平均匿名化耗时**，用于对比方法开销。

### 8.3 质量/稳定性指标

- 按 `error_type` 分组的报错数量趋势。
- 预检纠偏发生率（`precheck_info` 中发生纠偏的文件数 / 总文件数）。
- 分段损坏率、匿名化分段失败率、合并失败率。
- 静态丢弃率（`static_dropped_count / segment_count` 的分布）。

### 8.4 资源效率指标（可选，需 Worker 上报）

- 平均 CPU 使用率、峰值内存使用率（按任务记录采样均值/最大值）。
- GPU 可用时的匿名化加速比（对比 `--device cpu` 和 `--device cuda` 的历史平均耗时，若数据充足）。

## 9. 部署方案（Docker）

### 9.1 镜像设计

以 `python:3.12-slim` 为基础，安装 `ffmpeg`、`git`、`watchdog`、`fastapi`、`uvicorn`、`psutil`、`sqlalchemy` 等依赖，克隆 `video-privacy-blur` 并 `pip install -e .`，下载 YOLO 模型到镜像内 `models/` 目录（或在首次启动时下载到挂载的持久化卷）。

```dockerfile
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN git clone https://github.com/MengWoods/video-privacy-blur.git /opt/video-privacy-blur && \
    cd /opt/video-privacy-blur && pip install -e .
# 模型文件建议通过卷挂载或启动时下载，避免镜像过大
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 9.2 docker-compose 关键配置

```yaml
services:
  video-anonymizer:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - ./input:/data/input
      - ./output:/data/output
      - ./workdir:/data/workdir
      - ./models:/opt/video-privacy-blur/models
      - ./db:/data/db
    environment:
      - INPUT_PATH=/data/input
      - OUTPUT_PATH=/data/output
      - WORK_DIR=/data/workdir
      - DB_PATH=/data/db/tasks.sqlite
      - CPU_HIGH=85
      - MEM_HIGH=85
      - DISK_LOW_GB=5
      - KEEP_FAILED_INTERMEDIATE=false
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    # 如无 GPU，删除 deploy 块，设备设为 cpu
```

**GPU 注意事项**：Docker 中 FFmpeg/YOLO 使用 NVIDIA GPU 需要 `--gpus` 或 compose 的 `deploy.resources.reservations.devices` 配置，部分场景需要 `privileged` 或显式 capability。`video-privacy-blur` 的 `--device cuda` 依赖容器内可见 CUDA。若 GPU 不可用，`--device auto` 应回退到 CPU，但需确保 CUDA 探测不阻塞流水线。

**资源限制**：compose 中应设置 `mem_limit` 和 `cpus`，防止流水线峰值拖垮宿主机。建议初始值 `mem_limit: 8g`、`cpus: 4.0`，根据实际硬件调整。

### 9.3 持久化与恢复

SQLite 数据库、模型文件、`input`/`output`/`workdir` 均通过卷挂载持久化。容器重启后，后台 Worker 启动时扫描 `tasks` 表中 `status` 不为终态（`completed`/`failed`）的任务，根据其 `status` 从相应阶段**恢复执行**（例如 `segmenting` 状态的任务重新从分段阶段开始，已存在的分段文件跳过）。工作目录中的中间文件在恢复时按分段状态判断是否可复用；**若中间文件已被清理，则从对应阶段重新生成，不依赖中间文件存在**。源文件始终保留在 `input` 目录，恢复过程可随时重新读取。

## 10. 非功能性需求

| 类别 | 要求 |
|---|---|
| **单文件处理可靠性** | 预检纠偏覆盖率 100%（所有入队文件均经过 ffprobe 检查）；编码不兼容导致的硬失败率 < 5% |
| **流水线吞吐** | 在 4 核 / 8 GB 纯 CPU 环境下，对 10 分钟 1080p H.264 视频，端到端处理时间不超过视频时长的 3 倍（不含 GPU 加速） |
| **前端响应** | WebSocket 状态更新延迟 < 2s；统计查询在 10 万条任务记录下响应 < 1s（依赖索引和预聚合表） |
| **磁盘** | 稳态下 `workdir` 占用不超过当前处理中任务总输入大小的 2 倍；任务结束后 **`workdir` 中间过程文件清理率 100%**（**源文件与最终输出文件除外**，清理失败记录可查） |
| **数据安全** | 源文件与最终输出文件零误删、零篡改；清理器以任务工作子目录为边界执行删除 |
| **可观测性** | 每个失败有明确的 `stage` 和 `error_type`；前端可直接定位到源文件、分段和原始错误摘要 |

## 11. 风险与待确认项

| 风险 | 影响 | 缓解 |
|---|---|---|
| `video-privacy-blur` 模型对低分辨率/夜间车牌的召回率不足 | 车牌匿名化遗漏 | 暴露 `--conf` 和 `--scale` 供调优；统计中按 `settings_snapshot` 分组，便于对比不同参数的效果 |
| `mpdecimate` 对“缓慢运动但非静态”视频误判 | 有用内容被丢弃 | 默认阈值保守（保留帧 > 5% 即不丢弃）；阈值可配置；统计中展示静态丢弃率分布供人工复核 |
| 分段 `-c copy` 的时长不精确 | 与用户设置的分段时长有偏差 | 在 UI 中注明“实际时长受关键帧间隔影响”；提供 HLS 精确分段作为高级备选方案 |
| GPU 在 Docker 中的可用性依赖宿主机配置 | 匿名化回退到 CPU，吞吐下降 | 部署文档中明确 GPU 前置条件；`--device auto` 探测失败时记录 warning 并回退，不阻塞任务 |
| 马赛克默认值下，`--pixelate-blocks` 设置不当可能导致可辨识度残留 | 匿名化不充分 | 默认使用工具推荐块数；高级参数可调；统计中按匿名化方法分组，便于对马赛克方法的效果做人工抽检 |
| 失败任务的中间文件默认清理，导致排障困难 | 问题定位成本上升 | 报错记录中保留源文件路径、分段序号、stderr 摘要；提供 `KEEP_FAILED_INTERMEDIATE` 开关供临时保留 |

**待确认**：`video-privacy-blur` 的 `--blur-strength` / `--pixelate-blocks` 默认值在 README 中未给出具体数值，开发时需以实际 CLI `--help` 输出为准，PRD 中暂列为“工具默认值”，在设置面板中以只读默认值展示，高级用户可覆盖。