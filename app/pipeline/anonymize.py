"""
import subprocess
from pathlib import Path
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type,
)
from app.core.config import (
    BLUR_STRENGTH, PIXELATE_BLOCKS, ANONYMIZE_DEVICE,
    FACE_DETECTOR, MODELS_DIR, ANONYMIZE_MAX_RETRIES,
    ANONYMIZE_RETRY_BACKOFF,
)


class AnonymizeError(RuntimeError):
    pass


def _build_args(input_path: Path, output_path: Path, settings: dict) -> list[str]:
    blur_method = settings["blur_method"]
    anonymize_face = settings["anonymize_face"]
    anonymize_plate = settings["anonymize_plate"]

    args = [
        "privacy-blur",
        "--input", str(input_path),
        "--output", str(output_path),
        "--device", ANONYMIZE_DEVICE,#"--device", "cuda",# ← 从 ANONYMIZE_DEVICE 改为显式 cuda，跳过 auto 探测
        "--method", blur_method,
        "--blur-strength", str(BLUR_STRENGTH),
        #"--pixelate-blocks", str(PIXELATE_BLOCKS),
        #"--imgsz", 640,#YOLO推理尺寸，默认为960，如果分辨率低可以降低以提高推理速度 #"--imgsz", "640",            # ← 960 → 640，480p 视频足够
    ]
    if not anonymize_plate:
        args.append("--no-blur-plates")
    else:
        args += ["--plate-weights", str(MODELS_DIR / "license_plate_detector.pt")]

    if not anonymize_face:
        args.append("--no-blur-faces")
    elif FACE_DETECTOR == "yolo":
        args += [
            "--face-detector", "yolo",
            "--face-yolo-weights", str(MODELS_DIR / "yolov8n-face.pt"),
        ]
    return args


@retry(
    stop=stop_after_attempt(ANONYMIZE_MAX_RETRIES),
    wait=wait_exponential(multiplier=ANONYMIZE_RETRY_BACKOFF, min=2, max=30),
    retry=retry_if_exception_type(AnonymizeError),
    reraise=True,
)
def anonymize(input_path: Path, output_path: Path, settings: dict | None = None):
    if settings is None:
        from app.core.runtime_settings import get_settings
        settings = get_settings()

    args = _build_args(input_path, output_path, settings)
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise AnonymizeError(
            f"privacy-blur 失败 (code={result.returncode}): {result.stderr[-500:]}"
        )
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise AnonymizeError(f"输出文件为空: {output_path}")
"""
from pathlib import Path

from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type,
)

from app.core.config import ANONYMIZE_MAX_RETRIES, ANONYMIZE_RETRY_BACKOFF
from .blur.engine import process_video


class AnonymizeError(RuntimeError):
    pass


@retry(
    stop=stop_after_attempt(ANONYMIZE_MAX_RETRIES),
    wait=wait_exponential(multiplier=ANONYMIZE_RETRY_BACKOFF, min=2, max=30),
    retry=retry_if_exception_type(AnonymizeError),
    reraise=True,
)
def anonymize(input_path: Path, output_path: Path, settings: dict | None = None):
    if settings is None:
        from app.core.runtime_settings import get_settings
        settings = get_settings()

    try:
        process_video(input_path, output_path, settings)
    except Exception as e:
        # CUDA OOM 不可恢复，直接抛出避免无意义重试
        if "out of memory" in str(e).lower():
            raise
        raise AnonymizeError(f"匿名化失败: {e}") from e