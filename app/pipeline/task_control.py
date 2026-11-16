
import asyncio
import threading


class TaskStopped(Exception):
    """任务被用户停止。"""


class TaskCancelled(Exception):
    """任务被用户取消。"""


class TaskControl:
    def __init__(self):
        self.resume_event = threading.Event()
        self.resume_event.set()

        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()

        # 当前正在执行的任务阶段
        self.current_stage = "queued"

        # 保护暂停、恢复、停止、取消和阶段切换
        self.state_lock = threading.RLock()

    def pause(self):
        with self.state_lock:
            self._check_terminal()
            self.resume_event.clear()

    def resume(self):
        with self.state_lock:
            self._check_terminal()
            self.resume_event.set()

    def request_stop(self):
        with self.state_lock:
            self.stop_event.set()
            self.resume_event.set()

    def request_cancel(self):
        with self.state_lock:
            self.cancel_event.set()
            self.resume_event.set()

    def _check_terminal(self):
        if self.cancel_event.is_set():
            raise TaskCancelled("用户取消了任务")

        if self.stop_event.is_set():
            raise TaskStopped("用户停止了任务")

    def checkpoint(self):
        while True:
            self._check_terminal()

            if self.resume_event.wait(0.2):
                break

        self._check_terminal()

    async def checkpoint_async(self):
        while True:
            self._check_terminal()

            if self.resume_event.is_set():
                break

            await asyncio.sleep(0.2)

        self._check_terminal()

    def try_set_stage(self, status: str) -> bool:
        """
        原子地更新任务阶段。

        如果任务已经暂停，则不允许切换阶段。
        返回 False 表示需要等待恢复。
        """
        with self.state_lock:
            self._check_terminal()

            if not self.resume_event.is_set():
                return False

            self.current_stage = status
            return True
