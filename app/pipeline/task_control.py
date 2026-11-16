
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

        self.current_stage = "queued"

    def pause(self):
        self.resume_event.clear()

    def resume(self):
        self.resume_event.set()

    def request_stop(self):
        self.stop_event.set()
        self.resume_event.set()

    def request_cancel(self):
        self.cancel_event.set()
        self.resume_event.set()

    def _check_terminal(self):
        if self.cancel_event.is_set():
            raise TaskCancelled("用户取消了任务")

        if self.stop_event.is_set():
            raise TaskStopped("用户停止了任务")

    def checkpoint(self):
        self._check_terminal()

        while not self.resume_event.wait(0.2):
            self._check_terminal()

        self._check_terminal()

    async def checkpoint_async(self):
        self._check_terminal()

        while not self.resume_event.is_set():
            self._check_terminal()
            await asyncio.sleep(0.2)

        self._check_terminal()
