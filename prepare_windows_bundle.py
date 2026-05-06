#!/usr/bin/env python3
from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
EXPORTS_DIR = ROOT.parent / "exports"
BUNDLE_DIR = EXPORTS_DIR / "pupu_price_mvp_windows_bundle"


def reset_bundle_dir() -> None:
    if BUNDLE_DIR.exists():
        shutil.rmtree(BUNDLE_DIR)
    BUNDLE_DIR.mkdir(parents=True)


def copy_project_files() -> None:
    include_names = {
        "README.md",
        "run_mvp.py",
        "phase2_android_rpa_design.md",
        "requirements-windows.txt",
        "prepare_windows_bundle.py",
        "config",
        "windows",
    }
    for path in ROOT.iterdir():
        if path.name not in include_names:
            continue
        target = BUNDLE_DIR / path.name
        if path.is_dir():
            shutil.copytree(path, target)
        else:
            shutil.copy2(path, target)


def create_runtime_dirs() -> None:
    (BUNDLE_DIR / "logs").mkdir(parents=True, exist_ok=True)
    (BUNDLE_DIR / "screenshots").mkdir(parents=True, exist_ok=True)
    (BUNDLE_DIR / "logs" / ".gitkeep").write_text("", encoding="utf-8")
    (BUNDLE_DIR / "screenshots" / ".gitkeep").write_text("", encoding="utf-8")


def write_transfer_notes() -> None:
    notes = """# Windows Bundle Notes

复制这个目录到 Windows 后：

1. 安装 Python 3.10+
2. 安装 Node.js LTS
3. 安装并启动 MuMu / 雷电
4. 在模拟器里登录朴朴 App
5. 运行 windows/setup_windows_mvp.ps1

模板配置：

- windows/config.device.windows.mumu.json
- windows/config.appium_capabilities.windows.mumu.json

如需覆盖当前配置，可把模板内容复制到：

- config/device.json
- config/appium_capabilities.json
"""
    (BUNDLE_DIR / "WINDOWS_TRANSFER_NOTES.md").write_text(notes, encoding="utf-8")


def main() -> None:
    reset_bundle_dir()
    copy_project_files()
    create_runtime_dirs()
    write_transfer_notes()
    print(f"Windows bundle ready: {BUNDLE_DIR}")


if __name__ == "__main__":
    main()
