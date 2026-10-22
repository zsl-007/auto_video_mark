import psutil


def resource_ok() -> bool:
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory().percent
    return cpu < 85 and mem < 85