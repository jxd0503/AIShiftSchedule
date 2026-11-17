import os
import sys
from pathlib import Path
from typing import cast

APP_NAME = "AIShiftScheduleContest"


def resource_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(cast(str, getattr(sys, "_MEIPASS")))
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    override = os.environ.get("AISHIFT_DATA_DIR")
    path = Path(override) if override else Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    (path / "logs").mkdir(parents=True, exist_ok=True)
    return path
