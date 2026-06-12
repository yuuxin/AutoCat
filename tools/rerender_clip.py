#!/usr/bin/env python3
"""Re-render one saved clip script after content-sync correction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autokat.core.renderer import render_simple


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    error = render_simple(
        script, args.output, script["audio_path"],
        bgm_path=script.get("bgm_path"), fps=int(script.get("fps") or 30),
    )
    if error:
        raise SystemExit(error)


if __name__ == "__main__":
    main()
