import os
from pathlib import Path

BASE_DIR = Path(os.getenv("BASE_DIR", "/app"))

INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
WORKDIR = BASE_DIR / "workdir"
MODELS_DIR = BASE_DIR / "models"
DB_DIR = BASE_DIR / "data"
DB_PATH = DB_DIR / "video_anonymizer.db"

# 分段
SEGMENT_DURATION = int(os.getenv("SEGMENT_DURATION", "600"))
SEGMENT_ENABLED = os.getenv("SEGMENT_ENABLED", "true").lower() == "true"

# 静态检测
STATIC_THRESHOLD = float(os.getenv("STATIC_THRESHOLD", "0.5"))
MPDECIMATE_FRAC = os.getenv("MPDECIMATE_FRAC", "0.33")

# 匿名化
ANONYMIZE_FACE = os.getenv("ANONYMIZE_FACE", "true").lower() == "true"
ANONYMIZE_PLATE = os.getenv("ANONYMIZE_PLATE", "true").lower() == "true"
BLUR_METHOD = os.getenv("BLUR_METHOD", "pixelate")          # pixelate | gaussian
BLUR_STRENGTH = int(os.getenv("BLUR_STRENGTH", "15"))
PIXELATE_BLOCKS = int(os.getenv("PIXELATE_BLOCKS", "10"))
ANONYMIZE_DEVICE = os.getenv("ANONYMIZE_DEVICE", "auto")     # auto | cpu | cuda | mps
FACE_DETECTOR = os.getenv("FACE_DETECTOR", "yolo")           # haar | yolo

# 合并
MERGE_ENABLED = os.getenv("MERGE_ENABLED", "true").lower() == "true"

# 资源
MAX_WORKERS_PER_STAGE = int(os.getenv("MAX_WORKERS_PER_STAGE", "2"))
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