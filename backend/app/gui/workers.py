from PySide6.QtCore import QThread

# 正在运行的后台线程：退出应用前统一等待，避免 QThread 被销毁时仍在运行
_ACTIVE: list[QThread] = []


def register(thread: QThread) -> None:
    _ACTIVE.append(thread)


def unregister(thread: QThread) -> None:
    if thread in _ACTIVE:
        _ACTIVE.remove(thread)


def wait_all(timeout_ms: int = 15000) -> None:
    for thread in list(_ACTIVE):
        thread.wait(timeout_ms)
