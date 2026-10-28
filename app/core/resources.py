import subprocess
import threading
import time

import psutil

_gpu_lock = threading.Lock()
_gpu_cache = {"ts": 0.0, "used": None, "total": None}
_GPU_CACHE_TTL = 2.0


def _query_gpu_mem() -> tuple[int | None, int | None]:
    try:
        import torch
        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return int(total - free), int(total)
    except Exception:
        pass
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        info = pynvml.nvmlDeviceGetMemoryInfo(h)
        return int(info.used), int(info.total)
    except Exception:
        pass
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            line = r.stdout.strip().splitlines()[0]
            used, total = [int(x.strip()) for x in line.split(",")]
            return used * 1024 * 1024, total * 1024 * 1024
    except Exception:
        pass
    return None, None


def gpu_mem_ratio() -> float | None:
    now = time.time()
    with _gpu_lock:
        if now - _gpu_cache["ts"] < _GPU_CACHE_TTL:
            used, total = _gpu_cache["used"], _gpu_cache["total"]
        else:
            used, total = _query_gpu_mem()
            _gpu_cache.update(ts=now, used=used, total=total)
    if used is None or total is None or total == 0:
        return None
    return used / total


def gpu_mem_ok(threshold: float | None = None) -> bool:
    if threshold is None:
        try:
            from app.core.runtime_settings import get_settings
            threshold = float(get_settings().get("gpu_mem_threshold", 0.85))
        except Exception:
            threshold = 0.85
    ratio = gpu_mem_ratio()
    if ratio is None:
        return True
    return ratio < threshold


def resource_ok() -> bool:
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory().percent
    return cpu < 85 and mem < 85 and gpu_mem_ok()