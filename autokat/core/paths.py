"""Runtime paths shared by source and packaged app builds."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_ROOT = Path(os.environ.get("AUTOKAT_DATA_DIR", PROJECT_ROOT)).expanduser()
BUNDLED_ASSETS_ROOT = Path(
    os.environ.get("AUTOKAT_BUNDLED_ASSETS_DIR", DATA_ROOT / "assets")
).expanduser()
BUNDLED_MODELS_ROOT = Path(
    os.environ.get("AUTOKAT_MODEL_DIR", PROJECT_ROOT / "models")
).expanduser()

ASSETS_ROOT = DATA_ROOT / "assets"
TASKS_ROOT = DATA_ROOT / "tasks"
OUTPUT_ROOT = DATA_ROOT / "output"
