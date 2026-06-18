#!/usr/bin/env python3
"""AutoCat .app 包内运行时审计.

专门抓三类会导致干净机器闪退的问题 (Plan B 双架构打包必备门禁):

1. 路径泄漏 - 任何 Mach-O 链到构建机的 /opt/homebrew /usr/local /Users/ Cellar
   (典型症状: AutoCat-3.0.1 闪退, _sqlite3.so 链 /opt/homebrew/opt/sqlite/lib/libsqlite3.dylib)
2. 部署目标过高 - Mach-O LC_BUILD_VERSION 高于 macOS 14.0, 在 14 机器上无法启动
3. Python framework 脏源 - 嵌入的 Python.framework 是从 Homebrew 拷的, 而不是 python.org universal2

用法:
    python3 packaging/python_runtime_audit.py path/to/AutoCat.app
    python3 packaging/python_runtime_audit.py path/to/AutoCat.app --json

退出码:
    0  全部通过
    1  发现路径泄漏 (必须修复, 否则干净机器必崩)
    2  发现部署目标过高 (高严重)
    3  Python framework 来源不明 (中等严重)
    4  同时存在 1 和 2
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# 与 release-policy.toml 的 [native_audit] / [compatibility] 对齐
FORBIDDEN_PREFIXES = (
    "/opt/homebrew",
    "/usr/local",
    "/Users/",
)
FORBIDDEN_PATTERNS = (
    re.compile(r"/Cellar/"),
)
ALLOWED_PREFIXES = (
    "@rpath",
    "@loader_path",
    "@executable_path",
    "/System/Library/",
    "/usr/lib/",
    "/usr/lib/system/",
    "/Library/Frameworks/Python.framework/",
)
# PySide6 6.11.1 实际 deployment target 是 15.0, 这里与之对齐
MINIMUM_MACOS_VERSION = "15.0"


def _run_otool(binary: Path, mode: str) -> list[str]:
    result = subprocess.run(
        ["otool", mode, str(binary)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _is_mach_o(binary: Path) -> bool:
    return "Mach-O" in subprocess.run(
        ["file", str(binary)], capture_output=True, text=True,
    ).stdout


def _is_inside_app(dep: str, app_real: str) -> bool:
    """依赖路径是否在 .app bundle 内部, 内部绝对路径不视作泄漏."""
    return dep.startswith(app_real + "/") or dep == app_real


def _is_forbidden(dep: str, app_real: str | None = None) -> bool:
    if app_real and _is_inside_app(dep, app_real):
        return False
    if any(dep.startswith(p) for p in FORBIDDEN_PREFIXES):
        return True
    return any(p.search(dep) for p in FORBIDDEN_PATTERNS)


def _is_allowed(dep: str) -> bool:
    return any(dep.startswith(p) for p in ALLOWED_PREFIXES)


def _extract_deployment_target(binary: Path) -> str | None:
    lines = _run_otool(binary, "-l")
    capture = False
    for raw in lines:
        line = raw.strip()
        if line == "cmd LC_BUILD_VERSION":
            capture = True
            continue
        if capture and line.startswith("minos"):
            parts = line.split()
            if len(parts) >= 2:
                return parts[1]
            capture = False
        if capture and line.startswith("cmd "):
            capture = False
    return None


def _is_minimum_violation(binary: Path) -> str | None:
    target = _extract_deployment_target(binary)
    if target is None:
        return None
    try:
        target_tuple = tuple(int(x) for x in target.split("."))
        min_tuple = tuple(int(x) for x in MINIMUM_MACOS_VERSION.split("."))
    except ValueError:
        return target
    if target_tuple > min_tuple:
        return target
    return None


def _audit_python_framework_source(app_dir: Path) -> dict:
    py_framework = app_dir / "Contents" / "Resources" / "Python.framework"
    if not py_framework.exists():
        return {"status": "absent", "path": None}

    real_path = py_framework.resolve()
    real_str = str(real_path)

    if real_str.startswith("/Library/Frameworks/Python.framework"):
        return {"status": "clean", "path": real_str, "source": "python.org"}

    if real_str.startswith("/Users/") and real_str.endswith("/Library/Frameworks/Python.framework"):
        return {"status": "clean_user", "path": real_str, "source": "python.org (user-local)"}

    if "/Cellar/" in real_str or "/homebrew/" in real_str.lower():
        return {"status": "dirty", "path": real_str, "source": "homebrew"}

    return {"status": "unknown", "path": real_str, "source": "unknown"}


def _audit_architecture_purity(app_dir: Path) -> dict:
    """检查 .app 主要可执行文件是 arm64 单架构, 没有 x86_64 也没有 fat universal.

    当前策略 (Apple Silicon + macOS 14+) 是单架构, fat universal 意味着浪费空间.
    """
    candidates = [
        app_dir / "Contents" / "MacOS" / "AutoCat",
        app_dir / "Contents" / "Resources" / "Python.framework" / "Versions" / "Current" / "Python",
        app_dir / "Contents" / "Resources" / "bin" / "python3",
    ]
    results = []
    for binary in candidates:
        if not binary.exists() or binary.is_symlink():
            continue
        info = subprocess.run(
            ["file", str(binary)], capture_output=True, text=True,
        ).stdout
        if not info:
            continue
        is_arm64 = "arm64" in info
        is_x86_64 = "x86_64" in info
        if is_arm64 and not is_x86_64:
            results.append({"binary": str(binary.relative_to(app_dir)), "arch": "arm64", "ok": True})
        elif is_arm64 and is_x86_64:
            # universal2 在 Apple Silicon 上原生运行, 只警告不报错
            results.append({"binary": str(binary.relative_to(app_dir)), "arch": "universal2", "ok": True, "warn": True})
        else:
            results.append({"binary": str(binary.relative_to(app_dir)), "arch": "x86_64 or unknown", "ok": False})
    return {"binaries": results, "all_arm64": all(r["ok"] for r in results) if results else True}


def _iter_mach_o(app_dir: Path) -> Iterable[Path]:
    for path in sorted(app_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if _is_mach_o(path):
            yield path


def audit(app_dir: Path) -> dict:
    app_dir = app_dir.resolve()
    if not (app_dir / "Contents" / "Info.plist").exists():
        raise SystemExit(f"不是有效的 .app bundle: {app_dir}")

    findings = []
    summary = {
        "app": str(app_dir),
        "min_macos": MINIMUM_MACOS_VERSION,
        "mach_o_count": 0,
        "forbidden_dep_count": 0,
        "deployment_target_violations": 0,
        "non_arm64_binaries": 0,
        "python_framework": {},
        "verdict": "pass",
    }

    for binary in _iter_mach_o(app_dir):
        summary["mach_o_count"] += 1
        rel = str(binary.relative_to(app_dir))
        app_real = str(app_dir)

        for line in _run_otool(binary, "-L")[1:]:
            dep = line.strip().split(" (", 1)[0]
            if not dep or _is_allowed(dep):
                continue
            if _is_forbidden(dep, app_real=app_real):
                findings.append({
                    "severity": "error",
                    "kind": "forbidden_path",
                    "binary": rel,
                    "dependency": dep,
                })
                summary["forbidden_dep_count"] += 1

        over = _is_minimum_violation(binary)
        if over:
            findings.append({
                "severity": "error",
                "kind": "deployment_target",
                "binary": rel,
                "deployment_target": over,
                "min_required": MINIMUM_MACOS_VERSION,
            })
            summary["deployment_target_violations"] += 1

    summary["python_framework"] = _audit_python_framework_source(app_dir)
    arch_audit = _audit_architecture_purity(app_dir)
    summary["architecture_purity"] = arch_audit
    summary["non_arm64_binaries"] = sum(1 for b in arch_audit["binaries"] if not b["ok"] and not b.get("warn"))
    for bad in (b for b in arch_audit["binaries"] if not b["ok"] and not b.get("warn")):
        findings.append({
            "severity": "warn",
            "kind": "non_arm64_binary",
            "binary": bad["binary"],
            "arch": bad["arch"],
            "note": "当前策略 Apple Silicon 单架构, 出现 x86_64 或 fat 意味着 .app 含冗余切片",
        })
    for u2 in (b for b in arch_audit["binaries"] if b.get("warn")):
        findings.append({
            "severity": "info",
            "kind": "universal2_binary",
            "binary": u2["binary"],
            "arch": u2["arch"],
            "note": "universal2 (arm64+x86_64) Apple Silicon 原生运行, Intel 走 Rosetta",
        })
    if summary["python_framework"].get("status") == "dirty":
        findings.append({
            "severity": "error",
            "kind": "dirty_python_framework",
            "path": summary["python_framework"]["path"],
            "note": "Python.framework 来自 Homebrew Cellar, 在干净机器上必然崩",
        })

    has_path_leak = summary["forbidden_dep_count"] > 0
    has_target_violation = summary["deployment_target_violations"] > 0
    has_dirty_py = summary["python_framework"].get("status") == "dirty"

    if has_path_leak and has_target_violation:
        summary["verdict"] = "fail_path_and_target"
    elif not arch_audit["all_arm64"] and has_path_leak:
        summary["verdict"] = "fail_path_and_arch"
    elif not arch_audit["all_arm64"] and has_target_violation:
        summary["verdict"] = "fail_target_and_arch"
    elif has_path_leak:
        summary["verdict"] = "fail_path"
    elif has_target_violation:
        summary["verdict"] = "fail_target"
    elif not arch_audit["all_arm64"]:
        summary["verdict"] = "fail_non_arm64"
    elif has_dirty_py:
        summary["verdict"] = "warn_dirty_python"
    else:
        summary["verdict"] = "pass"

    summary["findings"] = findings
    return summary


def _print_report(summary: dict) -> None:
    app = summary["app"]
    print("=" * 60)
    print("AutoCat .app 运行时审计")
    print("=" * 60)
    print(f"  app           : {app}")
    print(f"  最低 macOS    : {summary['min_macos']}")
    print(f"  Mach-O 文件数 : {summary['mach_o_count']}")
    print(f"  路径泄漏      : {summary['forbidden_dep_count']}")
    print(f"  部署目标违规  : {summary['deployment_target_violations']}")
    py = summary["python_framework"]
    print(f"  Python FW     : {py.get('status')} ({py.get('path', 'n/a')})")
    bad = summary["non_arm64_binaries"]
    print(f"  非 arm64 切片 : {bad}")
    print(f"  总评          : {summary['verdict']}")
    print()

    if not summary["findings"]:
        print("未发现禁止路径或过高部署目标")
        return

    by_kind = {}
    for f in summary["findings"]:
        by_kind.setdefault(f["kind"], []).append(f)

    if "forbidden_path" in by_kind:
        print(f"禁止路径 ({len(by_kind['forbidden_path'])} 处):")
        for f in by_kind["forbidden_path"][:20]:
            print(f"   - {f['binary']}  ->  {f['dependency']}")
        if len(by_kind["forbidden_path"]) > 20:
            print(f"   ... 还有 {len(by_kind['forbidden_path']) - 20} 处, 用 --json 查看完整列表")
        print()

    if "deployment_target" in by_kind:
        print(f"部署目标过高 ({len(by_kind['deployment_target'])} 处, min={MINIMUM_MACOS_VERSION}):")
        for f in by_kind["deployment_target"][:20]:
            print(f"   - {f['binary']}  minos={f['deployment_target']}")
        if len(by_kind["deployment_target"]) > 20:
            print(f"   ... 还有 {len(by_kind['deployment_target']) - 20} 处")
        print()

    if "dirty_python_framework" in by_kind:
        f = by_kind["dirty_python_framework"][0]
        print(f"Python framework 脏源: {f['path']}")
        print("   改用 python.org 64-bit universal2 installer 安装 Python 3.12.x")
        print()

    if "non_arm64_binary" in by_kind:
        print(f"非 arm64 二进制 ({len(by_kind['non_arm64_binary'])} 处):")
        for f in by_kind["non_arm64_binary"]:
            print(f"   - {f['binary']}  arch={f['arch']}")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="AutoCat .app 包内运行时审计 (Plan B 双架构打包门禁)",
    )
    parser.add_argument("app", help=".app bundle 路径")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = parser.parse_args()

    summary = audit(Path(args.app))

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_report(summary)

    verdict = summary["verdict"]
    if verdict == "pass":
        return 0
    if verdict == "fail_non_arm64":
        return 5
    if verdict == "fail_path_and_arch":
        return 6
    if verdict == "fail_target_and_arch":
        return 7
    if verdict == "fail_path":
        return 1
    if verdict == "fail_target":
        return 2
    if verdict == "warn_dirty_python":
        return 3
    if verdict == "fail_path_and_target":
        return 4
    return 1


if __name__ == "__main__":
    sys.exit(main())
