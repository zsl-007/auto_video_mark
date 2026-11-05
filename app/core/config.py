import os
from pathlib import Path

BASE_DIR = Path(os.getenv("BASE_DIR", "/app"))

INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
WORKDIR = BASE_DIR / "workdir"
MODELS_DIR = BASE_DIR / "models"
DB_DIR = BASE_DIR / "data"
DB_PATH = DB_DIR / "video_anonymizer.db"
LICENSE_DIR = BASE_DIR / "license"

# 分段
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION", "600"))
SEGMENT_ENABLED = os.getenv("SEGMENT_ENABLED", "true").lower() == "true"

# 静态处理
STATIC_PROCESS_ENABLED = os.getenv("STATIC_PROCESS_ENABLED", "true").lower() == "true"
STATIC_PROCESS_THRESHOLD = float(os.getenv("STATIC_PROCESS_THRESHOLD", "0.4"))
STATIC_FRAME_DROP_ENABLED = os.getenv("STATIC_FRAME_DROP_ENABLED", "false").lower() == "true"
MPDECIMATE_FRAC = os.getenv("MPDECIMATE_FRAC", "0.33")

# 匿名化
ANONYMIZE_FACE = os.getenv("ANONYMIZE_FACE", "true").lower() == "true"
ANONYMIZE_PLATE = os.getenv("ANONYMIZE_PLATE", "true").lower() == "true"
BLUR_METHOD = os.getenv("BLUR_METHOD", "pixelate")          # pixelate | gaussian
BLUR_STRENGTH = int(os.getenv("BLUR_STRENGTH", "15"))
PIXELATE_BLOCKS = int(os.getenv("PIXELATE_BLOCKS", "10"))
ANONYMIZE_DEVICE = os.getenv("ANONYMIZE_DEVICE", "auto")     # auto | cpu | cuda | mps
FACE_DETECTOR = os.getenv("FACE_DETECTOR", "yolo")           # haar | yolo

# 人脸匿名化增强
FACE_BLUR_STRENGTH = int( os.getenv("FACE_BLUR_STRENGTH", "7"))     # 人脸模糊强度（越大越模糊） 1~10
FACE_EXPAND_SCALE = float( os.getenv("FACE_EXPAND_SCALE", "1.8"))   # 人脸打码范围（包含头发） 1~2.5

# 文字匿名化（yolov8s-text.pt）
ANONYMIZE_TEXT = os.getenv("ANONYMIZE_TEXT", "false").lower() == "true"
TEXT_CONF = float(os.getenv("TEXT_CONF", "0.25"))            # 文字检测置信度
TEXT_IOU_THRESHOLD = float(os.getenv("TEXT_IOU_THRESHOLD", "0.15"))  # 文字 NMS 阈值（越小越不易合并相邻文字）
TEXT_EXPAND_SCALE = float(os.getenv("TEXT_EXPAND_SCALE", "1.15"))    # 文字框扩张比例（文字框小，无需像车牌那样扩 1.35）

# 合并
MERGE_ENABLED = os.getenv("MERGE_ENABLED", "true").lower() == "true"

# 资源
MAX_WORKERS_PER_STAGE = int(os.getenv("MAX_WORKERS_PER_STAGE", "4"))
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", str(MAX_WORKERS_PER_STAGE)))
QUEUE_MAXSIZE = int(os.getenv("QUEUE_MAXSIZE", "20"))
RESOURCE_CHECK_INTERVAL = int(os.getenv("RESOURCE_CHECK_INTERVAL", "10"))

# 重试
ANONYMIZE_MAX_RETRIES = int(os.getenv("ANONYMIZE_MAX_RETRIES", "3"))
ANONYMIZE_RETRY_BACKOFF = float(os.getenv("ANONYMIZE_RETRY_BACKOFF", "2.0"))

# 文件稳定性检测
FILE_STABLE_CHECK_INTERVAL = float(os.getenv("FILE_STABLE_CHECK_INTERVAL", "2.0"))
FILE_STABLE_MAX_WAIT = int(os.getenv("FILE_STABLE_MAX_WAIT", "300"))

# 清理
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "3600"))
WORKDIR_MAX_AGE = int(os.getenv("WORKDIR_MAX_AGE", "86400"))

for d in (INPUT_DIR, OUTPUT_DIR, WORKDIR, MODELS_DIR, DB_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 前端上传
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi"}
MAX_UPLOAD_SIZE = int(os.getenv("MAX_UPLOAD_SIZE", str(10 * 1024 * 1024 * 1024)))  # 默认 10GB

# 上传残留清理
STAGING_MAX_AGE = int(os.getenv("STAGING_MAX_AGE", "3600"))  # 默认 1 小时

# 视频压缩（预检后、分段前，降低分辨率以加速后续处理）
VIDEO_COMPRESS_ENABLED = os.getenv("VIDEO_COMPRESS_ENABLED", "true").lower() == "true"
MIN_RESOLUTION = int(os.getenv("MIN_RESOLUTION", "480"))  # 目标像素高度，480 表示 480p

# 匿名化（进程内实现）
ANONYMIZE_IMGSZ = int(os.getenv("ANONYMIZE_IMGSZ", "960"))            # 640 适合 480p 压缩后视频    # 匿名化推理尺寸（新任务生效）
ANONYMIZE_BATCH_SIZE = int(os.getenv("ANONYMIZE_BATCH_SIZE", "2"))    # 1=逐帧；4~8 更吃显存    # 匿名化批大小（新任务生效）
ANONYMIZE_PARALLEL_SEGMENTS = int(os.getenv("ANONYMIZE_PARALLEL_SEGMENTS", "4"))    # 匿名化段级并发数（全局，重启生效）
GPU_MEM_THRESHOLD = float(os.getenv("GPU_MEM_THRESHOLD", "0.85"))   # GPU 显存水位阈值

# ByteTrack 多目标跟踪
TRACKING_ENABLED = (os.getenv("TRACKING_ENABLED", "true").lower() == "true")    # 启用多目标跟踪
TRACKING_TRACK_BUFFER = int(os.getenv("TRACKING_TRACK_BUFFER", "30"))       # 目标丢失保留帧数
TRACKING_HIGH_THRESH = float(os.getenv("TRACKING_HIGH_THRESH", "0.25"))     # 跟踪高置信度阈值
TRACKING_LOW_THRESH = float(os.getenv("TRACKING_LOW_THRESH", "0.10"))       # 跟踪低置信度阈值
TRACKING_NEW_TRACK_THRESH = float(os.getenv("TRACKING_NEW_TRACK_THRESH", "0.25"))   # 新目标创建阈值
TRACKING_MATCH_THRESH = float(os.getenv("TRACKING_MATCH_THRESH", "0.8"))    # 目标匹配阈值
TRACKING_SMOOTH_ALPHA = float(os.getenv("TRACKING_SMOOTH_ALPHA", "0.65"))   # 跟踪框平滑系数
TRACKING_PREDICT_LOST = (os.getenv("TRACKING_PREDICT_LOST", "true").lower() == "true")  # 目标漏检时继续模糊

# 人体匿名化
ANONYMIZE_PERSON = (os.getenv("ANONYMIZE_PERSON", "false").lower() == "true")
PERSON_ANONYMIZE_PART = "head"  # 人体匿名部位 body/head
PERSON_DETECTOR_CONF = float(os.getenv("PERSON_DETECTOR_CONF", "0.35"))     # 置信度

# 人体匿名化增强
PERSON_BLUR_STRENGTH = int(os.getenv("PERSON_BLUR_STRENGTH", "7"))  # 人体模糊强度 1~10
PERSON_HEAD_RATIO = float(os.getenv("PERSON_HEAD_RATIO", "0.30"))   # 头部区域占人体框高度比例
