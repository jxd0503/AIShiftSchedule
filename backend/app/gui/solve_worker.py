from PySide6.QtCore import QThread, Signal

from ..services import schedule_service
from . import workers


class SolveWorker(QThread):
    """段 A 同步求解放到子线程，避免阻塞界面。"""

    solved = Signal(object)  # schedule_service.SolveResult
    failed = Signal(str)

    def __init__(self, plan_id: int) -> None:
        super().__init__()
        self.plan_id = plan_id

    def run(self) -> None:  # noqa: D401 - QThread 入口
        workers.register(self)
        try:
            result = schedule_service.solve_first_phase(self.plan_id)
        except Exception as error:  # noqa: BLE001 - 线程内异常统一回传界面
            self.failed.emit(str(error))
        else:
            self.solved.emit(result)
        finally:
            workers.unregister(self)
