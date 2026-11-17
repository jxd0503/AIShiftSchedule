from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..errors import DomainError
from . import workers


class AiWorker(QThread):
    """把一次 services 层调用放到子线程，避免模型请求阻塞界面。"""

    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func: Callable[..., Any], *args: Any) -> None:
        super().__init__()
        self._func = func
        self._args = args

    def run(self) -> None:  # noqa: D401 - QThread 入口
        workers.register(self)
        try:
            result = self._func(*self._args)
        except DomainError as error:
            self.failed.emit(str(error))
        except Exception as error:  # noqa: BLE001 - 线程内异常统一回传界面
            self.failed.emit(f"操作失败：{error}")
        else:
            self.succeeded.emit(result)
        finally:
            workers.unregister(self)
