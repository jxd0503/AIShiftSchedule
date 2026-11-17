# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent

# OR-Tools：只打包 CP-SAT 求解所需内容。
# ortools.dll 依赖 .libs 下的 SCIP / HiGHS / protobuf 等原生库，必须一并带上；
# 其余求解器（线性规划包装、legacy CP、math_opt 等）的 Python 模块不再收集。
binaries = collect_dynamic_libs("ortools")

datas = [
    (str(ROOT / "backend" / "resources"), "backend/resources"),
]

hiddenimports = [
    "ortools.sat.python.cp_model",
    *collect_submodules("sqlmodel"),
    *collect_submodules("openai"),
    *collect_submodules("openpyxl"),
]

a = Analysis(
    [str(ROOT / "run_gui.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 已移除的 Web 层
        "fastapi",
        "uvicorn",
        "starlette",
        # 无关的标准库 GUI / 图像库
        "tkinter",
        "_tkinter",
        "turtle",
        "turtledemo",
        "PIL",
        "Pillow",
        # 未使用的科研 / 测试依赖
        "matplotlib",
        "scipy",
        "IPython",
        "pytest",
        # 本应用只使用 QtCore / QtGui / QtWidgets
        "PySide6.QtNetwork",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtVirtualKeyboard",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
    ],
    noarchive=False,
    optimize=0,
)

# 清理 PySide6 hook 全量收集带来的冗余 Qt 二进制：
# 只保留窗口平台插件、Windows 样式插件与 ico 图片格式插件。
_DROP_QT_PREFIXES = (
    "PySide6/Qt6Pdf",
    "PySide6/Qt6Qml",
    "PySide6/Qt6Quick",
    "PySide6/Qt6Svg",
    "PySide6/Qt6VirtualKeyboard",
    "PySide6/Qt6Network",
)
_DROP_QT_FILES = {
    "PySide6/opengl32sw.dll",
    "PySide6/QtNetwork.pyd",
    "PySide6/QtPdf.pyd",
    "PySide6/QtQml.pyd",
    "PySide6/QtQuick.pyd",
    "PySide6/QtSvg.pyd",
    "PySide6/QtVirtualKeyboard.pyd",
}
_KEEP_PLUGINS = {
    "PySide6/plugins/platforms/qwindows.dll",
    "PySide6/plugins/platforms/qoffscreen.dll",
    "PySide6/plugins/styles/qmodernwindowsstyle.dll",
    "PySide6/plugins/imageformats/qico.dll",
}


def _keep_binary(name):
    normalized = name.replace("\\", "/")
    if normalized.startswith("PySide6/translations/"):
        return False
    if normalized.startswith("PySide6/plugins/"):
        return normalized in _KEEP_PLUGINS
    if normalized in _DROP_QT_FILES:
        return False
    if normalized.startswith(_DROP_QT_PREFIXES):
        return False
    return True


a.binaries = [entry for entry in a.binaries if _keep_binary(entry[0])]
a.datas = [entry for entry in a.datas if _keep_binary(entry[0])]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AIShiftSchedule",
    icon=str(ROOT / "backend" / "resources" / "app_logo.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
