#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
REPORTS_DIR = BASE_DIR / "reports"
TOOLS_DIR = BASE_DIR / "tools"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

CITIES_PATH = CONFIG_DIR / "cities.json"
BRANDS_PATH = CONFIG_DIR / "brands.json"
DEVICE_PATH = CONFIG_DIR / "device.json"
REAL_DEVICE_PROFILE_PATH = CONFIG_DIR / "real_device_profile.json"
DEVICE_LOCAL_PATH = CONFIG_DIR / "device.local.json"
REAL_DEVICE_PROFILE_LOCAL_PATH = CONFIG_DIR / "real_device_profile.local.json"


@dataclass
class CityConfig:
    city: str
    city_alias: str
    address_keyword: str
    enabled: bool
    note: str
    address_candidates: list[str] = field(default_factory=list)


@dataclass
class BrandConfig:
    brand: str
    search_keyword: str
    enabled: bool


@dataclass
class DeviceConfig:
    platform: str
    automation_stack: str
    provider: str
    adb_serial: str
    appium_server_url: str
    screen_width: int
    screen_height: int
    orientation: str
    target_app: str
    target_mode: str
    notes: str


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def resolve_config_path(default_path: Path, local_override_path: Path) -> Path:
    if local_override_path.exists():
        return local_override_path
    return default_path


def ensure_directories() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def load_cities() -> list[CityConfig]:
    return [CityConfig(**item) for item in load_json(CITIES_PATH)]


def load_brands() -> list[BrandConfig]:
    return [BrandConfig(**item) for item in load_json(BRANDS_PATH)]


def load_device() -> DeviceConfig:
    path = resolve_config_path(DEVICE_PATH, DEVICE_LOCAL_PATH)
    return DeviceConfig(**load_json(path))


def load_real_device_profile() -> dict[str, Any]:
    path = resolve_config_path(REAL_DEVICE_PROFILE_PATH, REAL_DEVICE_PROFILE_LOCAL_PATH)
    return load_json(path)


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=True)
    except Exception:
        return None


def run_command_passthrough(args: list[str]) -> int:
    result = subprocess.run(args, text=True)
    return result.returncode


def adb_prefix(serial: str | None) -> list[str]:
    prefix = ["adb"]
    if serial:
        prefix.extend(["-s", serial])
    return prefix


def adb_devices() -> list[str]:
    if not command_exists("adb"):
        return []
    result = run_command(["adb", "devices"])
    if result is None:
        return []
    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        devices.append(line.split()[0])
    return devices


def adb_device_details() -> list[tuple[str, str]]:
    if not command_exists("adb"):
        return []
    result = run_command(["adb", "devices", "-l"])
    if result is None:
        return []
    details: list[tuple[str, str]] = []
    for line in result.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        serial = line.split()[0]
        details.append((serial, line))
    return details


def adb_shell(serial: str, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    return run_command(adb_prefix(serial) + ["shell"] + args)


def appium_server_reachable(server_url: str) -> bool:
    if not command_exists("curl"):
        return False
    status_url = server_url.rstrip("/") + "/status"
    result = run_command(["curl", "-fsS", status_url])
    if result is None:
        return False
    return "\"ready\"" in result.stdout


def list_missing_modules() -> list[str]:
    module_names = {
        "appium": "appium-python-client",
        "numpy": "numpy",
        "PIL": "Pillow",
        "selenium": "selenium",
    }
    missing: list[str] = []
    for import_name, package_name in module_names.items():
        try:
            __import__(import_name)
        except Exception:
            missing.append(package_name)
    return missing


def detect_android_sdk_root() -> Path | None:
    candidates = [
        Path.home() / "Library/Android/sdk",
        Path("/opt/homebrew/share/android-commandlinetools"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def detect_java_home() -> Path | None:
    candidates = [
        Path("/Applications/Android Studio.app/Contents/jbr/Contents/Home"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def detect_phone_model(serial: str) -> str | None:
    result = adb_shell(serial, ["getprop", "ro.product.model"])
    if result is None:
        return None
    value = result.stdout.strip()
    return value or None


def pupu_app_installed(serial: str) -> bool:
    result = adb_shell(serial, ["pm", "list", "packages", "com.pupumall.customer"])
    return result is not None and "com.pupumall.customer" in result.stdout


def enabled_cities() -> list[CityConfig]:
    return [city for city in load_cities() if city.enabled]


def enabled_brands() -> list[BrandConfig]:
    return [brand for brand in load_brands() if brand.enabled]


def pick_pilot_city(target_city: str | None) -> CityConfig:
    cities = enabled_cities()
    if target_city:
        for city in cities:
            if city.city == target_city:
                return city
        raise SystemExit(f"未找到匹配城市: {target_city}")
    if not cities:
        raise SystemExit("cities.json 中没有 enabled=true 的城市。")
    return cities[0]


def pick_pilot_brand(target_brand: str | None) -> BrandConfig:
    brands = enabled_brands()
    if target_brand:
        for brand in brands:
            if brand.brand == target_brand:
                return brand
        raise SystemExit(f"未找到匹配品牌: {target_brand}")
    if not brands:
        raise SystemExit("brands.json 中没有 enabled=true 的品牌。")
    return brands[0]


def resolve_serial(explicit_serial: str | None) -> str:
    if explicit_serial:
        return explicit_serial
    configured = load_device().adb_serial.strip()
    if configured:
        return configured
    devices = adb_devices()
    if len(devices) == 1:
        return devices[0]
    if not devices:
        raise SystemExit("当前没有可用 adb 设备，请先连接 Android 真机。")
    raise SystemExit("检测到多个 adb 设备，请显式提供 --serial。")


def build_capture_command(
    serial: str,
    city: str | None,
    brand: str | None,
    date_str: str,
    retries: int,
    capture_all: bool,
    dry_run: bool,
    strict: bool,
    fast: bool,
    overwrite: bool,
    retry_failed: bool,
    scroll_screenshot: bool,
    debug_recommendation: bool,
    output_mode: str,
    legacy_longshot: bool,
    all_brands: bool = False,
    city_first: bool = False,
) -> list[str]:
    cmd = [
        "python3",
        str(TOOLS_DIR / "run_daily_real_device_capture.py"),
        "--serial",
        serial,
        "--date",
        date_str,
        "--appium-server-url",
        load_device().appium_server_url,
        "--retries",
        str(retries),
    ]
    if all_brands:
        cmd.append("--all-enabled-brands")
    else:
        chosen_brand = pick_pilot_brand(brand)
        cmd.extend(["--brand", chosen_brand.brand])
    if city_first:
        cmd.append("--city-first")
    if capture_all:
        cmd.append("--all-enabled-cities")
    else:
        chosen_city = pick_pilot_city(city)
        cmd.extend(["--city", chosen_city.city])
    if dry_run:
        cmd.append("--dry-run")
    if strict:
        cmd.append("--strict")
    if fast:
        cmd.append("--fast")
    if overwrite:
        cmd.append("--overwrite")
    if retry_failed:
        cmd.append("--retry-failed")
    if scroll_screenshot:
        cmd.append("--scroll-screenshot")
    if output_mode:
        cmd.extend(["--output-mode", output_mode])
    if legacy_longshot:
        cmd.append("--legacy-longshot")
    if debug_recommendation:
        cmd.append("--debug-recommendation")
    return cmd


def write_all_brands_summary(date_str: str, brands: list[BrandConfig]) -> tuple[Path, Path, list[dict[str, str]]]:
    rows: list[dict[str, str]] = []
    for brand in brands:
        json_path = REPORTS_DIR / date_str / f"{brand.brand}_capture_summary.json"
        if not json_path.exists():
            rows.append(
                {
                    "date": date_str,
                    "brand": brand.brand,
                    "search_keyword": brand.search_keyword,
                    "city": "",
                    "target_city": "",
                    "address_keyword": "",
                    "address_candidates": "",
                    "attempted_address_candidates": "",
                    "selected_address_keyword": "",
                    "selected_address_text": "",
                    "address_candidate_status": "",
                    "delivery_available": "",
                    "status": "summary_missing",
                    "screenshot_path": "",
                    "failed_screenshot_path": "",
                    "started_at": "",
                    "finished_at": "",
                    "duration_seconds": "0.0",
                    "total_duration_seconds": "0.0",
                    "slowest_step": "",
                    "slowest_step_seconds": "",
                    "step_timings": {},
                    "error_message": f"missing brand summary: {json_path}",
                    "warning": "",
                    "mode": "",
                    "strict": "",
                    "fast": "",
                    "overwrite": "",
                    "retry_failed": "",
                    "result_screenshot_path": "",
                    "longshot_path": "",
                    "screenshot_output_mode": "",
                    "page_count": "",
                    "pages_folder_path": "",
                    "h5_image_path": "",
                    "scroll_ratio": "",
                    "overlap_ratio": "",
                    "transition_validation_enabled": "",
                    "transition_warnings": "",
                    "viewport_count": "",
                    "h5_stitch_mode": "",
                    "overlap_removal_applied": "",
                    "overlap_removal_method": "",
                    "overlap_removal_warnings": "",
                    "duplicate_content_reduced": "",
                    "floating_widget_handling": "",
                    "card_boundary_adjustment_applied": "",
                    "card_boundary_warnings": "",
                    "duplicate_overlap_kept": "",
                    "final_h5_image_height": "",
                    "original_page_count": "",
                    "final_section_count": "",
                    "native_longshot_supported": "",
                    "system_longshot_supported": "",
                    "native_longshot_image_path": "",
                    "system_longshot_raw_path": "",
                    "system_longshot_trigger_method": "",
                    "failure_reason": "",
                    "before_address_click_page_type": "",
                    "after_address_click_page_type": "",
                    "address_page_confirmed": "",
                    "address_keyword_input_allowed": "",
                    "city_switch_verified": "",
                    "selected_city_verified": "",
                    "address_match_warning": "",
                    "skipped": "false",
                }
            )
            continue
        data = load_json(json_path)
        if isinstance(data, list):
            rows.extend(item for item in data if isinstance(item, dict))

    report_dir = REPORTS_DIR / date_str
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "all_brands_capture_summary.csv"
    json_path = report_dir / "all_brands_capture_summary.json"
    fieldnames = [
        "date",
        "brand",
        "search_keyword",
        "city",
        "target_city",
        "address_keyword",
        "address_candidates",
        "attempted_address_candidates",
        "selected_address_keyword",
        "selected_address_text",
        "address_candidate_status",
        "delivery_available",
        "status",
        "screenshot_path",
        "failed_screenshot_path",
        "started_at",
        "finished_at",
        "duration_seconds",
        "total_duration_seconds",
        "slowest_step",
        "slowest_step_seconds",
        "step_timings",
        "error_message",
        "warning",
        "mode",
        "strict",
        "fast",
        "overwrite",
        "retry_failed",
        "result_screenshot_path",
        "longshot_path",
        "screenshot_output_mode",
        "page_count",
        "pages_folder_path",
        "h5_image_path",
        "scroll_ratio",
        "overlap_ratio",
        "transition_validation_enabled",
        "transition_warnings",
        "viewport_count",
        "h5_stitch_mode",
        "overlap_removal_applied",
        "overlap_removal_method",
        "overlap_removal_warnings",
        "duplicate_content_reduced",
        "floating_widget_handling",
        "card_boundary_adjustment_applied",
        "card_boundary_warnings",
        "duplicate_overlap_kept",
        "final_h5_image_height",
        "original_page_count",
        "final_section_count",
        "native_longshot_supported",
        "system_longshot_supported",
        "native_longshot_image_path",
        "system_longshot_raw_path",
        "system_longshot_trigger_method",
        "failure_reason",
        "screenshot_mode",
        "is_longshot",
        "shot_count",
        "max_shots",
        "viewport_width",
        "viewport_height",
        "image_width",
        "image_height",
        "crop_applied",
        "crop_reason",
        "crop_y",
        "stop_reason",
        "reached_page_bottom",
        "reached_recommendation_section",
        "maybe_truncated",
        "recommendation_detect_method",
        "recommendation_detect_text",
        "recommendation_detect_confidence",
        "recommendation_detect_shot_index",
        "bottom_detection_method",
        "before_address_click_page_type",
        "after_address_click_page_type",
        "address_page_confirmed",
        "address_keyword_input_allowed",
        "city_switch_verified",
        "selected_city_verified",
        "address_match_warning",
        "skipped",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path, rows


def print_all_brands_summary(rows: list[dict[str, str]], csv_path: Path, json_path: Path) -> None:
    success = [row for row in rows if row.get("status") == "success"]
    skipped = [row for row in rows if row.get("status") == "skipped_existing"]
    failed = [row for row in rows if row.get("status") not in {"success", "skipped_existing"}]
    total_duration = sum(float(row.get("duration_seconds") or 0) for row in rows)
    print()
    print("All brands capture summary")
    print(f"- success rows: {len(success)}")
    print(f"- skipped_existing rows: {len(skipped)}")
    print(f"- failed rows: {len(failed)}")
    print(f"- total_duration_seconds: {total_duration:.1f}")
    if failed:
        failed_text = ", ".join(
            f"{row.get('brand', '')}/{row.get('city', '')}({row.get('status', '')})"
            for row in failed[:20]
        )
        suffix = " ..." if len(failed) > 20 else ""
        print(f"- failed: {failed_text}{suffix}")
    slow_entries: list[tuple[float, str, str, str]] = []
    for row in rows:
        timings = row.get("step_timings")
        if isinstance(timings, dict):
            for step, seconds in timings.items():
                try:
                    value = float(seconds)
                except Exception:
                    continue
                if value > 0:
                    slow_entries.append((value, row.get("city", ""), row.get("brand", ""), step))
    if slow_entries:
        slow_entries.sort(reverse=True)
        print("- top slow steps:")
        for seconds, city, brand, step in slow_entries[:5]:
            print(f"  {city}/{brand}/{step}: {seconds:.3f}s")
    else:
        print("- top slow steps: none")
    print(f"- csv: {csv_path}")
    print(f"- json: {json_path}")


def print_config_summary() -> None:
    device = load_device()
    city = pick_pilot_city(None)
    brand = pick_pilot_brand(None)
    profile = load_real_device_profile()

    print("MVP route")
    print("- Mac + Android 真机 + 朴朴 APP + Appium/UIAutomator2")
    print()
    print("Pilot case")
    print(f"- city: {city.city}")
    print(f"- brand: {brand.brand}")
    print(f"- search keyword: {brand.search_keyword}")
    print(f"- address keyword: {city.address_keyword or '<manual>'}")
    print()
    print("Device")
    print(
        f"- provider={device.provider}, platform={device.platform}, "
        f"stack={device.automation_stack}, app={device.target_app}, mode={device.target_mode}"
    )
    print(f"- adb_serial={device.adb_serial or '<unset>'}")
    print(f"- appium_server_url={device.appium_server_url}")
    print(f"- device_name={profile.get('device_name', '<unset>')}")
    print(f"- screen={profile.get('screen_width')}x{profile.get('screen_height')}")


def print_plan(target_city: str | None, target_brand: str | None) -> None:
    city = pick_pilot_city(target_city)
    brand = pick_pilot_brand(target_brand)
    device = load_device()

    print("Real-device capture plan")
    print()
    print("Target")
    print(f"- city: {city.city}")
    print(f"- brand: {brand.brand}")
    print(f"- search keyword: {brand.search_keyword}")
    print(f"- address keyword: {city.address_keyword or '<manual>'}")
    print()
    print("Execution path")
    print("- 连接 Android 真机并保持朴朴 APP 已登录")
    print("- 启动 Appium Server")
    print("- Appium / UIAutomator2 进入首页并打开地址入口")
    print(f"- 切换到 {city.city}")
    print(f"- 使用固定地址关键词 {city.address_keyword or '<manual>'} 选择可配送点位")
    print(f"- 从首页通用搜索框搜索 {brand.search_keyword}")
    print("- 生成结果页长截图")
    print("- 结果页校验通过后才按 品牌/日期/城市 命名规则归档")
    print()
    print("Current config")
    print(
        f"- provider={device.provider}, stack={device.automation_stack}, target_app={device.target_app}"
    )


def print_doctor() -> None:
    device = load_device()
    missing_modules = list_missing_modules()
    devices = adb_devices()
    device_details = adb_device_details()
    appium_ok = appium_server_reachable(device.appium_server_url)
    sdk_root = detect_android_sdk_root()
    java_home = detect_java_home()
    serial = None
    try:
        serial = resolve_serial(None)
    except SystemExit:
        serial = None
    model = detect_phone_model(serial) if serial else None
    pupu_ok = pupu_app_installed(serial) if serial else False

    print("Android real-device doctor")
    print()
    print("Host")
    print(f"- python: {sys.version.split()[0]}")
    print(f"- adb: {'ok' if command_exists('adb') else 'missing'}")
    print(f"- appium server: {'ok' if appium_ok else 'missing/unreachable'}")
    print(
        f"- python modules: {'ok' if not missing_modules else 'missing -> ' + ', '.join(missing_modules)}"
    )
    print(f"- java home: {java_home or 'missing'}")
    print(f"- android sdk root: {sdk_root or 'missing'}")
    print()
    print("Route")
    print("- locked: Mac + Android real device + 朴朴 APP")
    print("- deprecated: desktop WeChat mini-program")
    print("- deprecated: Mac Android Emulator")
    print()
    print("ADB devices")
    if device_details:
        for _, detail in device_details:
            print(f"- {detail}")
    else:
        print("- none connected")
    print()
    print("Target app")
    print(f"- resolved serial: {serial or 'unresolved'}")
    print(f"- phone model: {model or 'unknown'}")
    print(f"- pupu installed: {'ok' if pupu_ok else 'missing'}")
    print()
    print("Assessment")
    ready = command_exists("adb") and appium_ok and not missing_modules and bool(devices) and pupu_ok
    if ready:
        print("- 环境已可执行真机长截图流程。")
    else:
        print("- 当前还不能稳定执行真机长截图流程。")
    print()
    print("Next commands")
    if not command_exists("adb"):
        print("- brew install android-platform-tools")
    if missing_modules:
        print(
            "- python3 -m pip install --user Appium-Python-Client opencv-python adbutils uiautomator2 pillow"
        )
    if not java_home:
        print("- 安装 Android Studio 或提供可用 JAVA_HOME")
    if not sdk_root:
        print("- 安装 Android SDK platform-tools 和相关运行时")
    if not appium_ok:
        print(f"- bash {TOOLS_DIR / 'start_appium_server.sh'}")
    if not devices:
        print("- 连接 Android 真机并确认 adb devices 可见")
    if devices and not pupu_ok:
        print("- 在手机上安装并登录朴朴 APP")
    if ready:
        print("- python3 run_mvp.py --capture-one --city 福州市 --brand 卫龙")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="朴朴价格巡检真机 MVP")
    parser.add_argument("--city", type=str, help="指定城市，例如: 佛山市")
    parser.add_argument("--brand", type=str, help="指定品牌，例如: 卫龙")
    parser.add_argument("--all-brands", action="store_true", help="按 brands.json 中 enabled=true 的品牌逐个执行")
    parser.add_argument(
        "--city-first",
        action="store_true",
        help="城市优先矩阵模式：每个城市只切换一次，然后连续采集多个品牌",
    )
    parser.add_argument("--serial", type=str, help="指定 adb serial，默认优先读取 device.json")
    parser.add_argument("--date", type=str, default=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
    parser.add_argument("--retries", type=int, default=0, help="单城市失败后的重试次数")
    parser.add_argument("--print-config", action="store_true", help="打印当前真机 MVP 配置")
    parser.add_argument("--plan", action="store_true", help="打印 1 城市 + 1 品牌执行计划")
    parser.add_argument("--doctor", action="store_true", help="检查 Appium/ADB/真机环境")
    parser.add_argument("--capture-one", action="store_true", help="执行 1 城市 + 1 品牌真机截图")
    parser.add_argument("--capture-all", action="store_true", help="执行所有 enabled 城市的真机截图")
    parser.add_argument("--dry-run", action="store_true", help="只打印执行目标，不真正操作手机")
    parser.add_argument("--strict", action="store_true", help="严格模式：仅校验通过的结果页进入正式目录")
    parser.add_argument("--fast", action="store_true", help="快速模式：减少保守等待，但保留 strict 校验")
    parser.add_argument("--overwrite", action="store_true", help="覆盖当天同城市同品牌已有正式截图")
    parser.add_argument("--retry-failed", action="store_true", help="仅重跑当天最新 summary 中失败城市")
    parser.add_argument("--scroll-screenshot", action="store_true", help="旧参数：显式启用拼接长图")
    parser.add_argument(
        "--output-mode",
        choices=["native-longshot", "system-longshot", "h5", "pages", "h5-pages", "viewport", "longshot"],
        default="",
        help="截图交付模式；默认使用配置，当前正式交付为 pages",
    )
    parser.add_argument("--legacy-longshot", "--stitch-longshot", action="store_true", help="显式使用旧拼接长图模式")
    parser.add_argument("--debug-recommendation", action="store_true", help="保存逐屏推荐区 OCR/模板检测调试图")
    parser.add_argument(
        "--print-start-appium",
        action="store_true",
        help="打印推荐的 Appium 启动命令",
    )
    return parser


def main() -> None:
    ensure_directories()
    args = build_parser().parse_args()

    if args.print_config:
        print_config_summary()
        return

    if args.plan:
        print_plan(args.city, args.brand)
        return

    if args.doctor:
        print_doctor()
        return

    if args.print_start_appium:
        print(f"bash {TOOLS_DIR / 'start_appium_server.sh'}")
        return

    if args.capture_one or args.capture_all:
        serial = resolve_serial(args.serial)
        if args.all_brands and args.city_first:
            brands = enabled_brands()
            if not brands:
                raise SystemExit("brands.json 中没有 enabled=true 的品牌。")
            cmd = build_capture_command(
                serial=serial,
                city=args.city,
                brand=None,
                date_str=args.date,
                retries=args.retries,
                capture_all=args.capture_all,
                dry_run=args.dry_run,
                strict=args.strict,
                fast=args.fast,
                overwrite=args.overwrite,
                retry_failed=args.retry_failed,
                scroll_screenshot=args.scroll_screenshot,
                debug_recommendation=args.debug_recommendation,
                output_mode=args.output_mode,
                legacy_longshot=args.legacy_longshot,
                all_brands=True,
                city_first=True,
            )
            exit_code = run_command_passthrough(cmd)
            if not args.dry_run:
                csv_path, json_path, rows = write_all_brands_summary(args.date, brands)
                print_all_brands_summary(rows, csv_path, json_path)
            raise SystemExit(exit_code)

        if args.all_brands:
            if args.capture_one:
                raise SystemExit("--all-brands 如需单城市执行，请配合 --city-first 使用")
            brands = enabled_brands()
            if not brands:
                raise SystemExit("brands.json 中没有 enabled=true 的品牌。")
            exit_codes: list[int] = []
            for brand in brands:
                print()
                print(f"=== Capture brand: {brand.brand} ===")
                cmd = build_capture_command(
                    serial=serial,
                    city=None,
                    brand=brand.brand,
                    date_str=args.date,
                    retries=args.retries,
                    capture_all=True,
                    dry_run=args.dry_run,
                    strict=args.strict,
                    fast=args.fast,
                    overwrite=args.overwrite,
                    retry_failed=args.retry_failed,
                    scroll_screenshot=args.scroll_screenshot,
                    debug_recommendation=args.debug_recommendation,
                    output_mode=args.output_mode,
                    legacy_longshot=args.legacy_longshot,
                )
                exit_codes.append(run_command_passthrough(cmd))
            if not args.dry_run:
                csv_path, json_path, rows = write_all_brands_summary(args.date, brands)
                print_all_brands_summary(rows, csv_path, json_path)
            raise SystemExit(1 if any(code != 0 for code in exit_codes) else 0)

        cmd = build_capture_command(
            serial=serial,
            city=args.city,
            brand=args.brand,
            date_str=args.date,
            retries=args.retries,
            capture_all=args.capture_all,
            dry_run=args.dry_run,
            strict=args.strict,
            fast=args.fast,
            overwrite=args.overwrite,
            retry_failed=args.retry_failed,
            scroll_screenshot=args.scroll_screenshot,
            debug_recommendation=args.debug_recommendation,
            output_mode=args.output_mode,
            legacy_longshot=args.legacy_longshot,
        )
        raise SystemExit(run_command_passthrough(cmd))

    print("Real-device MVP entry is ready.")
    print("Try one of the following:")
    print("  python3 run_mvp.py --print-config")
    print("  python3 run_mvp.py --plan --city 福州市 --brand 卫龙")
    print("  python3 run_mvp.py --doctor")
    print("  python3 run_mvp.py --capture-one --city 福州市 --brand 卫龙 --dry-run")


if __name__ == "__main__":
    main()
