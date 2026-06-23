# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir specification for AutoCat Windows builds (Windows 10 1903+ / x86_64).

用法（PowerShell）：
    $env:AUTOKAT_APP_VERSION = "3.0.1"
    pyinstaller packaging/AutoCat-win.spec --distpath dist --workpath build

输出：dist/AutoCat/  (NSIS 会把它打包成 AutoCat-3.0.1-windows-x86_64.exe)
"""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


project = Path(os.environ.get("SPECPATH", "")).parent.resolve()

app_version = os.environ.get("AUTOKAT_APP_VERSION", "3.0.1")
icon_path = os.environ.get("AUTOKAT_ICON_PATH", "")

local_model_mode = os.environ.get("AUTOKAT_LOCAL_MODEL_MODE", "bundled")
if local_model_mode not in {"bundled", "download"}:
    raise ValueError(f"Unsupported AUTOKAT_LOCAL_MODEL_MODE: {local_model_mode}")

# ── 数据文件 ──────────────────────────────────────────────
datas = [
    (str(project / "models" / "mobileclip_s0_image.onnx"), "models"),
    (str(project / "models" / "mobileclip_s0_labels.npz"), "models"),
]

if local_model_mode == "bundled":
    qwen_dir = project / "models" / "Qwen2.5-0.5B-Instruct"
    if qwen_dir.exists():
        datas.append((str(qwen_dir), "models/Qwen2.5-0.5B-Instruct"))

bundled_bgm = project / "assets" / "bgm"
if bundled_bgm.is_dir():
    for f in bundled_bgm.iterdir():
        if f.is_file():
            datas.append((str(f), "assets/bgm"))

# ── 隐藏导入（edge-tts 有大量子模块）──────────────────────
hiddenimports = collect_submodules("edge_tts") + [
    "typing",
    "typing_extensions",
]

a = Analysis(
    [str(project / "autokat" / "__main__.py")],
    pathex=[str(project)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "IPython", "pytest",
        "tkinter", "test", "unittest",
    ],
    noarchive=False,
    console=False,      # Windows GUI app
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoCat",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    icon=icon_path if icon_path else None,
    version=os.environ.get("AUTOKAT_WIN_VERSION_INFO", ""),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="AutoCat",
)
