#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
from appium import webdriver
from appium.options.android import UiAutomator2Options
from PIL import Image
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
SCREENSHOT_PAGES_DIR = BASE_DIR / "screenshots_pages"
DELIVERABLES_H5_DIR = BASE_DIR / "deliverables_h5"
NATIVE_LONGSHOT_DIR = BASE_DIR / "native_longshots"
FAILED_SCREENSHOT_DIR = BASE_DIR / "screenshots_failed"
LOG_DIR = BASE_DIR / "logs"
REPORTS_DIR = BASE_DIR / "reports"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
CITIES_PATH = CONFIG_DIR / "cities.json"
BRANDS_PATH = CONFIG_DIR / "brands.json"
REAL_DEVICE_PROFILE_PATH = CONFIG_DIR / "real_device_profile.json"
REAL_DEVICE_PROFILE_LOCAL_PATH = CONFIG_DIR / "real_device_profile.local.json"


@dataclass
class CityConfig:
    city: str
    city_alias: str
    address_keyword: str
    enabled: bool
    note: str


@dataclass
class BrandConfig:
    brand: str
    search_keyword: str
    enabled: bool


@dataclass
class ValidationResult:
    ok: bool
    status: str
    reason: str


@dataclass
class ScreenshotArtifact:
    path: Path
    metadata: dict[str, Any]
    cleanup_dir: Path | None = None


@dataclass
class RecommendationDetection:
    found: bool
    method: str = ""
    text: str = ""
    confidence: float = 0.0
    crop_y: int = 0
    warning: str = ""


class CaptureFailure(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class SessionBroken(Exception):
    """Raised when the Appium/UiAutomator2 session is no longer usable."""


class RunLogger:
    def __init__(self, date_str: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path = LOG_DIR / f"run_{date_str}.log"

    def log(self, city: str, stage: str, message: str) -> None:
        ts = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{city}] [{stage}] {message}"
        print(line)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


StepTimings = dict[str, float]


@contextmanager
def timed_step(timings: StepTimings, name: str):
    started = time.perf_counter()
    try:
        yield
    finally:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - started)


def add_timing(timings: StepTimings, name: str, seconds: float) -> None:
    timings[name] = timings.get(name, 0.0) + max(0.0, seconds)


def merge_timings(*items: StepTimings | None) -> StepTimings:
    merged: StepTimings = {}
    for item in items:
        if not item:
            continue
        for key, value in item.items():
            merged[key] = merged.get(key, 0.0) + float(value)
    return merged


def rounded_timings(timings: StepTimings | None) -> StepTimings:
    return {key: round(float(value), 3) for key, value in (timings or {}).items()}


def slowest_step_from_timings(timings: StepTimings | None) -> tuple[str, float]:
    if not timings:
        return "", 0.0
    step, seconds = max(timings.items(), key=lambda item: float(item[1]))
    return step, float(seconds)


def wait_until(condition, timeout_sec: float, interval_sec: float = 0.5) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() <= deadline:
        try:
            if condition():
                return True
        except WebDriverException as exc:
            if is_session_error(exc):
                raise SessionBroken(str(exc)) from exc
        except Exception:
            pass
        wait(interval_sec)
    return False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_config_path(default_path: Path, local_override_path: Path) -> Path:
    if local_override_path.exists():
        return local_override_path
    return default_path


def adb_prefix(serial: str | None) -> list[str]:
    prefix = ["adb"]
    if serial:
        prefix.extend(["-s", serial])
    return prefix


def adb_run(serial: str | None, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(adb_prefix(serial) + args, check=True, text=True, capture_output=True)


def adb_tap(serial: str | None, x: int, y: int) -> None:
    adb_run(serial, ["shell", "input", "tap", str(x), str(y)])


def adb_keyevent(serial: str | None, keycode: int) -> None:
    adb_run(serial, ["shell", "input", "keyevent", str(keycode)])


def adb_back(serial: str | None) -> None:
    adb_keyevent(serial, 4)


def adb_screencap(serial: str | None, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as f:
        subprocess.run(adb_prefix(serial) + ["exec-out", "screencap", "-p"], check=True, stdout=f)


def launch_pupu_app(serial: str | None) -> None:
    adb_run(
        serial,
        [
            "shell",
            "monkey",
            "-p",
            "com.pupumall.customer",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
    )
    wait(3)


def restart_pupu_app(serial: str | None, profile: dict[str, Any], logger: RunLogger, city: str) -> None:
    try:
        adb_run(serial, ["shell", "am", "force-stop", "com.pupumall.customer"])
        logger.log(city, "app", "force-stopped pupu app before relaunch")
    except Exception as exc:
        logger.log(city, "app", f"force-stop pupu app failed: {exc}")
    wait(1)
    launch_pupu_app(serial)
    wait(float(profile.get("session", {}).get("app_launch_wait_sec", 6)))


SESSION_ERROR_TOKENS = [
    "UiAutomation not connected",
    "Instrumentation run failed",
    "uiautomator2.GatewayError",
    "JSONRPCError",
    "Connection reset",
    "device offline",
    "adb device offline",
    "dump_hierarchy failed",
    "session broken",
    "HTTPConnectionPool",
    "Read timed out",
    "timed out",
    "socket hang up",
    "Appium Settings app is not running",
]


def is_session_error(exc: Exception | str) -> bool:
    message = str(exc)
    return any(token.lower() in message.lower() for token in SESSION_ERROR_TOKENS)


def load_cities() -> list[CityConfig]:
    return [CityConfig(**item) for item in load_json(CITIES_PATH)]


def load_brands() -> list[BrandConfig]:
    return [BrandConfig(**item) for item in load_json(BRANDS_PATH)]


def pick_city(target_city: str) -> CityConfig:
    for city in load_cities():
        if city.city == target_city:
            return city
    raise SystemExit(f"未找到城市配置: {target_city}")


def pick_brand(target_brand: str) -> BrandConfig:
    for brand in load_brands():
        if brand.brand == target_brand:
            return brand
    raise SystemExit(f"未找到品牌配置: {target_brand}")


def final_archive_path(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return SCREENSHOT_DIR / brand.brand / date_str / f"{brand.brand}（{city.city_alias} {date_str}）.png"


def final_pages_dir(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return SCREENSHOT_PAGES_DIR / brand.brand / date_str / city.city


def final_pages_first_path(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return final_pages_dir(city, brand, date_str) / f"{brand.brand}（{city.city_alias} {date_str}）_01.png"


def final_h5_dir(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return DELIVERABLES_H5_DIR / brand.brand / date_str / city.city


def final_h5_path(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return final_h5_dir(city, brand, date_str) / f"{brand.brand}（{city.city_alias} {date_str}）H5长图.png"


def final_native_longshot_dir(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return NATIVE_LONGSHOT_DIR / brand.brand / date_str / city.city


def final_native_longshot_path(city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    return final_native_longshot_dir(city, brand, date_str) / f"{brand.brand}（{city.city_alias} {date_str}）_system_raw.png"


def now_iso() -> str:
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def enabled_cities() -> list[CityConfig]:
    return [city for city in load_cities() if city.enabled]


def enabled_brands() -> list[BrandConfig]:
    return [brand for brand in load_brands() if brand.enabled]


def connect_driver(serial: str, device_name: str, server_url: str) -> webdriver.Remote:
    caps = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:deviceName": device_name,
        "appium:udid": serial,
        "appium:noReset": True,
        "appium:newCommandTimeout": 180,
    }
    return webdriver.Remote(server_url, options=UiAutomator2Options().load_capabilities(caps))


def appium_status_ready(server_url: str) -> bool:
    status_url = server_url.rstrip("/") + "/status"
    result = subprocess.run(["curl", "-fsS", status_url], text=True, capture_output=True)
    return result.returncode == 0 and "\"ready\":true" in result.stdout.replace(" ", "")


def safe_driver_quit(driver: webdriver.Remote | None) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def reset_uiautomator2(serial: str, logger: RunLogger, city: str, round_index: int) -> None:
    logger.log(city, "session", f"restarting uiautomator2 session round={round_index}")
    packages = [
        "io.appium.uiautomator2.server",
        "io.appium.uiautomator2.server.test",
        "io.appium.settings",
    ]
    for package in packages:
        try:
            adb_run(serial, ["shell", "am", "force-stop", package])
            logger.log(city, "session", f"force-stopped {package}")
        except Exception as exc:
            logger.log(city, "session", f"force-stop {package} failed: {exc}")
    wait(2)


def current_focus(serial: str) -> str:
    try:
        result = adb_run(serial, ["shell", "dumpsys", "window"])
    except Exception:
        return ""
    lines = [
        line.strip()
        for line in result.stdout.splitlines()
        if "mCurrentFocus" in line or "mFocusedApp" in line
    ]
    return "\n".join(lines)


def ensure_app_foreground(serial: str, profile: dict[str, Any], logger: RunLogger, city: str) -> None:
    focus = current_focus(serial)
    if "com.pupumall.customer" in focus:
        logger.log(city, "app", "pupu app already foreground")
        return
    logger.log(city, "app", f"pupu app not foreground, launching; focus={focus or '<unknown>'}")
    launch_pupu_app(serial)
    wait(float(profile.get("session", {}).get("app_launch_wait_sec", 6)))


def connect_driver_with_log(
    serial: str,
    profile: dict[str, Any],
    server_url: str,
    logger: RunLogger,
    city: str,
) -> webdriver.Remote:
    logger.log(city, "session", "connecting appium/uiautomator2 driver")
    driver = connect_driver(serial, profile["device_name"], server_url)
    try:
        _ = unique_texts(driver)
        logger.log(city, "session", "session health check ok")
    except SessionBroken:
        safe_driver_quit(driver)
        raise
    return driver


def apply_runtime_options(
    profile: dict[str, Any],
    fast: bool,
    scroll_screenshot: bool,
    debug_recommendation: bool = False,
    output_mode: str = "",
    legacy_longshot: bool = False,
) -> dict[str, Any]:
    profile = dict(profile)
    profile["_fast"] = fast
    profile["_debug_recommendation"] = debug_recommendation
    screenshot = dict(profile.get("screenshot") if isinstance(profile.get("screenshot"), dict) else {})
    default_mode = str(screenshot.get("default_mode", "pages")).lower()
    if legacy_longshot or scroll_screenshot:
        resolved_mode = "longshot"
    elif output_mode:
        resolved_mode = output_mode.lower()
    else:
        resolved_mode = default_mode
    if resolved_mode == "system-longshot":
        resolved_mode = "native-longshot"
    if resolved_mode in {"stitch", "legacy-longshot"}:
        resolved_mode = "longshot"
    if resolved_mode not in {"native-longshot", "h5", "pages", "h5-pages", "viewport", "longshot"}:
        resolved_mode = "pages"
    screenshot["default_mode"] = resolved_mode
    profile["screenshot"] = screenshot
    profile["_output_mode"] = resolved_mode
    profile["_scroll_screenshot"] = resolved_mode == "longshot"
    profile["_pages_screenshot"] = resolved_mode in {"h5", "pages", "h5-pages"}
    profile["_h5_pages"] = resolved_mode == "h5-pages"
    profile["_h5_only"] = resolved_mode == "h5"
    profile["_native_longshot"] = resolved_mode == "native-longshot"

    popup = dict(profile.get("popup", {}))
    if fast:
        popup["max_dismiss_rounds"] = min(int(popup.get("max_dismiss_rounds", 5)), 3)
    profile["popup"] = popup

    session = dict(profile.get("session", {}))
    if fast:
        session["app_launch_wait_sec"] = min(float(session.get("app_launch_wait_sec", 6)), 4)
    profile["session"] = session

    longshot = dict(profile.get("longshot", {}))
    if fast and "pause_sec" in longshot:
        longshot["pause_sec"] = max(0.8, float(longshot["pause_sec"]) * 0.75)
    profile["longshot"] = longshot
    return profile


def wait(seconds: float) -> None:
    time.sleep(seconds)


def unique_texts(driver: webdriver.Remote) -> list[str]:
    try:
        source = driver.page_source
    except WebDriverException as exc:
        if is_session_error(exc):
            raise SessionBroken(str(exc)) from exc
        raise
    texts = re.findall(r'text="([^"]{0,120})"', source)
    uniq: list[str] = []
    for item in texts:
        if item and item not in uniq:
            uniq.append(item)
    return uniq


def page_text_blob(driver: webdriver.Remote) -> str:
    return "\n".join(unique_texts(driver))


def click_xpath_if_exists(driver: webdriver.Remote, xpath: str) -> bool:
    els = driver.find_elements(By.XPATH, xpath)
    if els:
        els[0].click()
        return True
    return False


def click_text_or_desc(driver: webdriver.Remote, text: str) -> bool:
    xpath = f"//*[@text='{text}' or @content-desc='{text}']"
    return click_xpath_if_exists(driver, xpath)


def profile_point(profile: dict[str, Any], name: str) -> tuple[int, int]:
    point = profile[name]
    return int(point["x"]), int(point["y"])


def ratio_point(profile: dict[str, Any], point: dict[str, Any]) -> tuple[int, int]:
    width = int(profile["screen_width"])
    height = int(profile["screen_height"])
    x_ratio = point.get("x_ratio", point.get("x"))
    y_ratio = point.get("y_ratio", point.get("y"))
    return int(width * float(x_ratio)), int(height * float(y_ratio))


def load_profile_lists(profile: dict[str, Any], key: str) -> list[Any]:
    value = profile.get(key, [])
    return value if isinstance(value, list) else []


def popup_config(profile: dict[str, Any]) -> dict[str, Any]:
    popup = profile.get("popup")
    if not isinstance(popup, dict):
        popup = {}
    close_texts = popup.get("close_texts") or load_profile_lists(profile, "popup_close_texts")
    blocking_texts = popup.get("blocking_texts") or load_profile_lists(profile, "popup_blocking_texts")
    close_points = popup.get("close_points_ratio") or load_profile_lists(profile, "popup_close_points")
    close_descriptions = popup.get("close_descriptions") or ["close", "Close", "关闭", "x", "X"]
    avoid_click_texts = popup.get("avoid_click_texts") or ["去使用", "立即领取", "领取"]
    return {
        "close_texts": close_texts if isinstance(close_texts, list) else [],
        "blocking_texts": blocking_texts if isinstance(blocking_texts, list) else [],
        "close_points": close_points if isinstance(close_points, list) else [],
        "close_descriptions": close_descriptions if isinstance(close_descriptions, list) else [],
        "avoid_click_texts": avoid_click_texts if isinstance(avoid_click_texts, list) else [],
        "max_dismiss_rounds": int(popup.get("max_dismiss_rounds", 5)),
        "use_back_key": bool(popup.get("use_back_key", True)),
    }


def screenshot_config(profile: dict[str, Any]) -> dict[str, Any]:
    cfg = profile.get("screenshot")
    if not isinstance(cfg, dict):
        cfg = {}
    longshot_cfg = cfg.get("longshot")
    if not isinstance(longshot_cfg, dict):
        longshot_cfg = {}
    recommendation_texts = cfg.get("recommendation_texts_as_warning") or cfg.get(
        "recommendation_blocking_texts"
    )
    recommendation_texts = longshot_cfg.get("recommendation_texts") or recommendation_texts
    if not isinstance(recommendation_texts, list):
        recommendation_texts = ["猜你喜欢", "相关推荐", "热销推荐", "你可能还喜欢", "为你推荐"]
    longshot_stop_texts = cfg["longshot_stop_texts"] if "longshot_stop_texts" in cfg else recommendation_texts
    if not isinstance(longshot_stop_texts, list):
        longshot_stop_texts = recommendation_texts
    bottom_texts = longshot_cfg.get("bottom_texts") or cfg.get("bottom_texts")
    if not isinstance(bottom_texts, list):
        bottom_texts = ["没有更多了", "已经到底了", "到底了", "暂无更多", "没有更多商品"]
    template_cfg = longshot_cfg.get("template_matching")
    if not isinstance(template_cfg, dict):
        template_cfg = {}
    local_ocr_cfg = longshot_cfg.get("local_ocr")
    if not isinstance(local_ocr_cfg, dict):
        local_ocr_cfg = {}
    return {
        "default_mode": str(cfg.get("default_mode", "viewport")),
        "force_top_before_capture": bool(cfg.get("force_top_before_capture", True)),
        "recommendation_texts_as_warning": recommendation_texts,
        "do_not_crop_recommendation_area": bool(cfg.get("do_not_crop_recommendation_area", True)),
        "longshot_stop_texts": longshot_stop_texts,
        "longshot_bottom_texts": bottom_texts,
        "longshot_stop_crop_margin": int(cfg.get("longshot_stop_crop_margin", 24)),
        "longshot_min_stop_crop_height": int(cfg.get("longshot_min_stop_crop_height", 520)),
        "longshot_bottom_stable_rounds": int(longshot_cfg.get("bottom_stable_rounds", 2)),
        "save_only_one_final_image": bool(longshot_cfg.get("save_only_one_final_image", True)),
        "allow_recommendation_crop": bool(longshot_cfg.get("allow_recommendation_crop", True)),
        "crop_fail_keep_full_longshot": bool(longshot_cfg.get("crop_fail_keep_full_longshot", True)),
        "min_safe_crop_height_ratio": float(longshot_cfg.get("min_safe_crop_height_ratio", 1.2)),
        "save_only_complete_longshot_to_official": bool(longshot_cfg.get("save_only_complete_longshot_to_official", True)),
        "incomplete_goes_to_failed": bool(longshot_cfg.get("incomplete_goes_to_failed", True)),
        "local_ocr_enabled": bool(local_ocr_cfg.get("enabled", True)),
        "local_ocr_min_confidence": float(local_ocr_cfg.get("min_confidence", 0.8)),
        "local_ocr_chunk_height": int(local_ocr_cfg.get("chunk_height", 3600)),
        "local_ocr_chunk_overlap": int(local_ocr_cfg.get("chunk_overlap", 320)),
        "template_enabled": bool(template_cfg.get("enabled", True)),
        "template_dir": str(template_cfg.get("template_dir", "assets/templates")),
        "template_min_confidence": float(template_cfg.get("min_confidence", 0.85)),
        "recommendation_stop_mode": str(longshot_cfg.get("recommendation_stop_mode", "crop_above")),
        "crop_recommendation_section": bool(longshot_cfg.get("crop_recommendation_section", True)),
        "allow_recommendation_in_last_viewport": bool(longshot_cfg.get("allow_recommendation_in_last_viewport", False)),
    }


def current_popup_blockers(driver: webdriver.Remote, profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    texts = unique_texts(driver)
    blob = "\n".join(texts)
    blocking_texts = popup_config(profile)["blocking_texts"]
    matched = [text for text in blocking_texts if text in blob]
    generic_tokens = {"神券", "活动", "限时", "领取"}
    if matched and all(text in generic_tokens for text in matched):
        matched = []
    return texts, matched


def click_desc_if_exists(driver: webdriver.Remote, desc: str) -> bool:
    xpath = f"//*[@content-desc='{desc}']"
    return click_xpath_if_exists(driver, xpath)


def dismiss_startup_campaign_popups(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
    stage: str,
    max_rounds: int | None = None,
) -> bool:
    cfg = popup_config(profile)
    rounds = max_rounds or cfg["max_dismiss_rounds"]

    def log_after_strategy(strategy: str) -> bool:
        _, blockers_after = current_popup_blockers(driver, profile)
        if blockers_after:
            logger.log(city, stage, f"dismiss strategy={strategy}, popup still blocking: {blockers_after}")
            return False
        logger.log(city, stage, f"dismiss strategy={strategy}, popup cleared")
        return True

    for round_index in range(1, rounds + 1):
        texts, matched_blockers = current_popup_blockers(driver, profile)
        logger.log(city, stage, f"popup scan round={round_index}, blockers={matched_blockers or 'none'}")
        if not matched_blockers:
            return True

        clicked = False
        for text in cfg["close_texts"]:
            if text in cfg["avoid_click_texts"]:
                logger.log(city, stage, f"dismiss strategy=text_close skip avoid text: {text}")
                continue
            if text in texts and click_text_or_desc(driver, text):
                logger.log(city, stage, f"dismiss strategy=text_close clicked: {text}")
                wait(1)
                clicked = True
                if log_after_strategy("text_close"):
                    return True
                break
        if not clicked:
            logger.log(city, stage, "dismiss strategy=text_close failed")

        clicked = False
        for desc in cfg["close_descriptions"]:
            if click_desc_if_exists(driver, desc):
                logger.log(city, stage, f"dismiss strategy=description_close clicked: {desc}")
                wait(1)
                clicked = True
                if log_after_strategy("description_close"):
                    return True
                break
        if not clicked:
            logger.log(city, stage, "dismiss strategy=description_close failed")

        for point in cfg["close_points"]:
            x, y = ratio_point(profile, point)
            adb_tap(serial, x, y)
            logger.log(city, stage, f"dismiss strategy=ratio_point {point.get('name', '<unnamed>')} clicked ({x},{y})")
            wait(0.9)
            if log_after_strategy(f"ratio_point:{point.get('name', '<unnamed>')}"):
                return True

        if cfg["use_back_key"]:
            adb_back(serial)
            logger.log(city, stage, "dismiss strategy=back clicked")
            wait(1.2)
            if log_after_strategy("back"):
                return True

        width = int(profile["screen_width"])
        height = int(profile["screen_height"])
        blank_points = [(int(width * 0.08), int(height * 0.12)), (int(width * 0.50), int(height * 0.92))]
        for x, y in blank_points:
            adb_tap(serial, x, y)
            logger.log(city, stage, f"dismiss strategy=blank_area clicked ({x},{y})")
            wait(0.8)
            if log_after_strategy("blank_area"):
                return True

    texts, matched_blockers = current_popup_blockers(driver, profile)
    logger.log(city, stage, f"popup still blocking after retries: {matched_blockers or texts[:10]}")
    return False


def dismiss_common_popups(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
    stage: str,
    rounds: int | None = None,
) -> bool:
    return dismiss_startup_campaign_popups(driver, serial, profile, logger, city, stage, rounds)


def ensure_clean_page(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
    stage: str,
) -> None:
    if not dismiss_common_popups(driver, serial, profile, logger, city, stage):
        raise CaptureFailure("popup_blocked", f"{stage} 阶段弹层无法关闭")


def focus_address_input(driver: webdriver.Remote, profile: dict[str, Any], serial: str) -> None:
    click_xpath_if_exists(driver, "//*[@text='请输入你的收货地址']")

    def address_input_ready() -> bool:
        texts = unique_texts(driver)
        return find_search_input(driver) is not None or "取消" in texts or "搜索" in texts

    if wait_until(address_input_ready, 1.2, 0.3):
        return

    x, y = profile_point(profile, "address_input_focus")
    adb_tap(serial, x, y)
    if wait_until(address_input_ready, 2.5, 0.3):
        return
    wait(0.8)


def address_selector_ready(driver: webdriver.Remote) -> bool:
    texts = unique_texts(driver)
    return "选择收货地址" in texts and ("请输入你的收货地址" in texts or "地图选点" in texts)


def open_city_list(driver: webdriver.Remote) -> bool:
    if click_text_or_desc(driver, "福州市"):
        wait(1.5)
        return True
    return False


def select_city(driver: webdriver.Remote, city_name: str) -> None:
    if not click_text_or_desc(driver, city_name):
        raise CaptureFailure("city_failed", f"城市列表中未找到 {city_name}")
    wait(1.8)


def input_keyword(driver: webdriver.Remote, keyword: str) -> None:
    try:
        el = driver.find_element(By.XPATH, "//*[@class='android.widget.EditText']")
        el.click()
        try:
            el.clear()
        except WebDriverException:
            pass
        el.send_keys(keyword)
    except WebDriverException:
        driver.set_clipboard_text(keyword)
        driver.press_keycode(279)
    wait(2.5)


def pick_address_result_by_index(serial: str, profile: dict[str, Any], index: int) -> None:
    point = profile["address_first_result"]
    step_y = profile["address_result_step_y"]
    adb_tap(serial, int(point["x"]), int(point["y"] + index * step_y))
    wait(1.2)


def homepage_ready(driver: webdriver.Remote) -> bool:
    texts = unique_texts(driver)
    home_nav_hits = sum(1 for token in ["首页", "分类", "购物车", "我的"] if token in texts)
    category_hits = sum(
        1
        for token in ["时令好物", "水果鲜花", "蔬菜豆制品", "肉禽蛋", "海鲜水产", "粮油调味"]
        if token in texts
    )
    return home_nav_hits >= 2 or category_hits >= 2


def go_home(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
) -> None:
    ensure_clean_page(driver, serial, profile, logger, city, "go_home.start")
    if homepage_ready(driver):
        return

    for step in range(3):
        adb_back(serial)
        wait(1.2)
        ensure_clean_page(driver, serial, profile, logger, city, f"go_home.back_{step+1}")
        if homepage_ready(driver):
            return

    restart_pupu_app(serial, profile, logger, city)
    ensure_clean_page(driver, serial, profile, logger, city, "go_home.relaunch")
    if not homepage_ready(driver):
        for step in range(2):
            adb_tap(serial, 70, 145)
            logger.log(city, "go_home", f"fallback tap top-left back step={step+1}")
            wait(1.5)
            ensure_clean_page(driver, serial, profile, logger, city, f"go_home.top_left_{step+1}")
            if homepage_ready(driver):
                return
        raise CaptureFailure("app_failed", "无法回到朴朴首页")


def open_address_selector(
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
) -> None:
    x, y = profile_point(profile, "home_location_entry")
    adb_tap(serial, x, y)
    logger.log(city, "address", f"tap address entry at ({x},{y})")
    wait(2)


def open_search(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
) -> None:
    ensure_clean_page(driver, serial, profile, logger, city, "search.open")
    texts = unique_texts(driver)
    if find_search_input(driver) is not None or any(token in texts for token in ["历史搜索", "猜你想搜", "热门搜索", "取消"]):
        logger.log(city, "search.open", "search page already open")
        return

    def search_page_ready() -> bool:
        current_texts = unique_texts(driver)
        return find_search_input(driver) is not None or any(
            token in current_texts for token in ["历史搜索", "猜你想搜", "热门搜索", "取消"]
        )

    if "搜索" in texts and click_xpath_if_exists(driver, "//*[@text='搜索']"):
        logger.log(city, "search.open", "opened search by visible 搜索 button")
        if wait_until(search_page_ready, 5 if profile.get("_fast") else 7, 0.5):
            return
        wait(1.5)
        return

    bounds = profile["store_home_search_icon_bounds"]
    for el in driver.find_elements(By.XPATH, "//*[@clickable='true']"):
        rect = el.rect
        if (
            bounds["x_min"] <= rect["x"] <= bounds["x_max"]
            and bounds["y_min"] <= rect["y"] <= bounds["y_max"]
            and bounds["width_min"] <= rect["width"] <= bounds["width_max"]
            ):
            el.click()
            logger.log(city, "search.open", "opened search by clickable bounds match")
            if wait_until(search_page_ready, 5 if profile.get("_fast") else 7, 0.5):
                return
            wait(1.5)
            return

    width = int(profile["screen_width"])
    x, y = width // 2, 226
    adb_tap(serial, x, y)
    logger.log(city, "search.open", f"fallback tap search bar at ({x},{y})")
    if wait_until(search_page_ready, 5 if profile.get("_fast") else 7, 0.5):
        return
    wait(1.5)
    texts = unique_texts(driver)
    if find_search_input(driver) is not None or "历史搜索" in texts or "取消" in texts:
        return

    raise CaptureFailure("search_failed", "未找到搜索入口")


def find_search_input(driver: webdriver.Remote):
    els = driver.find_elements(By.XPATH, "//*[@class='android.widget.EditText']")
    return els[0] if els else None


def get_search_input_text(driver: webdriver.Remote) -> str:
    el = find_search_input(driver)
    if el is None:
        return ""
    try:
        value = (el.text or "").strip()
    except WebDriverException:
        value = ""
    return value


def clear_search_input(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
) -> None:
    focus_x, focus_y = profile_point(profile, "search_input_focus")
    clear_x, clear_y = profile_point(profile, "search_clear")

    for attempt in range(1, 4):
        ensure_clean_page(driver, serial, profile, logger, city, f"search.clear_{attempt}")
        adb_tap(serial, focus_x, focus_y)
        wait(0.4)
        current = get_search_input_text(driver)
        logger.log(city, "search.clear", f"attempt={attempt}, current_text={current or '<empty>'}")
        if not current:
            return

        adb_tap(serial, clear_x, clear_y)
        wait(0.4)
        current = get_search_input_text(driver)
        if not current:
            logger.log(city, "search.clear", "cleared by clear button")
            return

        try:
            el = find_search_input(driver)
            if el is not None:
                el.click()
                try:
                    el.clear()
                except WebDriverException:
                    pass
        except WebDriverException:
            pass
        wait(0.4)

        for _ in range(18):
            adb_keyevent(serial, 67)
        wait(0.5)
        current = get_search_input_text(driver)
        if not current:
            logger.log(city, "search.clear", "cleared by delete keyevents")
            return

    adb_back(serial)
    wait(0.8)
    open_search(driver, serial, profile, logger, city)
    current = get_search_input_text(driver)
    if current:
        raise CaptureFailure("search_failed", f"搜索框残留旧词，无法清空: {current}")


def input_search_keyword(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
    keyword: str,
) -> None:
    clear_search_input(driver, serial, profile, logger, city)
    focus_x, focus_y = profile_point(profile, "search_input_focus")
    adb_tap(serial, focus_x, focus_y)
    wait(0.4)

    el = find_search_input(driver)
    try:
        if el is not None:
            el.click()
    except WebDriverException:
        pass

    driver.set_clipboard_text(keyword)
    driver.press_keycode(279)
    wait(1)
    current = get_search_input_text(driver)
    logger.log(city, "search.input", f"after paste input_text={current or '<empty>'}")
    if keyword not in current:
        try:
            el = find_search_input(driver)
            if el is not None:
                el.click()
                el.send_keys(keyword)
                wait(1)
                current = get_search_input_text(driver)
                logger.log(city, "search.input", f"after send_keys input_text={current or '<empty>'}")
        except WebDriverException as exc:
            if is_session_error(exc):
                raise SessionBroken(str(exc)) from exc
            logger.log(city, "search.input", f"send_keys fallback failed: {exc}")

    if keyword not in current:
        raise CaptureFailure("search_failed", f"输入关键词后输入框状态异常: {current or '<empty>'}")


def validate_result_page(
    driver: webdriver.Remote,
    profile: dict[str, Any],
    brand: BrandConfig,
    logger: RunLogger,
    city: str,
    strict: bool,
) -> ValidationResult:
    texts = unique_texts(driver)
    blob = "\n".join(texts)
    popup_blocking_texts = popup_config(profile)["blocking_texts"]
    if any(token in texts for token in popup_blocking_texts):
        return ValidationResult(False, "popup_blocked", "页面仍被活动弹层覆盖")

    out_of_range_tokens = ["超出配送范围", "当前地址暂不支持配送", "无法配送", "超区"]
    if any(token in blob for token in out_of_range_tokens):
        return ValidationResult(False, "out_of_range", "当前地址超出配送范围")

    empty_tokens = ["暂无结果", "没有找到", "暂无商品", "未找到相关商品"]
    if any(token in blob for token in empty_tokens):
        return ValidationResult(False, "empty_result", "品牌搜索无结果")

    city_tokens = ["选择收货地址", "当前定位", "选择城市", "请输入你的收货地址"]
    if any(token in blob for token in city_tokens):
        return ValidationResult(False, "city_failed", "当前仍停留在地址/城市选择流")

    suggestion_tokens = ["搜索历史", "猜你想搜", "热门搜索", "历史搜索"]
    homepage_tokens = ["首页", "分类", "购物车", "我的"]

    keyword_present = brand.search_keyword in blob
    price_present = bool(re.search(r"[¥￥]\s*\d", blob) or re.search(r"\b\d+\.\d{1,2}\b", blob))
    homepage_like = sum(1 for token in homepage_tokens if token in texts) >= 3
    suggestion_like = any(token in blob for token in suggestion_tokens)
    screenshot_cfg = screenshot_config(profile)
    recommendation_hits = [
        token
        for token in screenshot_cfg["recommendation_texts_as_warning"]
        if token in blob
    ]

    logger.log(
        city,
        "validate",
        f"keyword_present={keyword_present}, price_present={price_present}, "
        f"homepage_like={homepage_like}, suggestion_like={suggestion_like}, "
        f"recommendation_hits={recommendation_hits or 'none'}",
    )

    if homepage_like:
        return ValidationResult(False, "invalid_result_page", "当前仍停留在首页")

    if suggestion_like and not price_present:
        return ValidationResult(False, "invalid_result_page", "当前仍停留在搜索联想页")

    if recommendation_hits and (not keyword_present or not price_present):
        return ValidationResult(False, "invalid_result_page", "当前疑似停留在尾部推荐区")

    if not keyword_present:
        if strict:
            return ValidationResult(False, "invalid_result_page", "页面缺少品牌关键词")
        return ValidationResult(False, "invalid_result_page", "页面缺少品牌关键词")

    if not price_present:
        return ValidationResult(False, "invalid_result_page", "页面未检测到价格结构")

    return ValidationResult(True, "success", "已通过结果页校验")


def wait_for_result_page_stable(
    driver: webdriver.Remote,
    profile: dict[str, Any],
    brand: BrandConfig,
    logger: RunLogger,
    city: str,
    strict: bool,
    timeout_sec: float | None = None,
) -> ValidationResult:
    timeout = timeout_sec if timeout_sec is not None else (5 if profile.get("_fast") else 8)
    deadline = time.time() + timeout
    last_validation = ValidationResult(False, "invalid_result_page", "结果页尚未稳定")
    last_blob = ""
    stable_hits = 0

    while time.time() <= deadline:
        validation = validate_result_page(driver, profile, brand, logger, city, strict)
        texts = unique_texts(driver)
        blob = "\n".join(texts)
        loading = any(token in blob for token in ["加载中", "正在加载", "请稍候"])
        if validation.ok and not loading:
            if blob == last_blob:
                stable_hits += 1
            else:
                stable_hits = 1
                last_blob = blob
            if stable_hits >= 2:
                logger.log(city, "validate", "result page stable")
                return validation
        else:
            last_validation = validation
            last_blob = blob
            stable_hits = 0
        wait(0.8 if profile.get("_fast") else 1.2)

    return last_validation


def trigger_search(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
    brand: BrandConfig,
    strict: bool,
) -> None:
    submit_cfg = profile.get("search_submit_point", {"x_ratio": 0.90, "y_ratio": 0.07})
    try:
        x, y = ratio_point(profile, submit_cfg)
        logger.log(city, "search.submit", f"trigger search by top-right button ratio at ({x},{y})")
        adb_tap(serial, x, y)
        wait(1 if profile.get("_fast") else 2)
        ensure_clean_page(driver, serial, profile, logger, city, "search.submit.top_right")
        validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
        if validation.ok:
            return
    except Exception as exc:
        logger.log(city, "search.submit", f"top-right search tap failed: {exc}")

    keyboard_submit_cfg = profile.get("keyboard_search_submit_point", {"x_ratio": 0.92, "y_ratio": 0.92})
    try:
        x, y = ratio_point(profile, keyboard_submit_cfg)
        logger.log(city, "search.submit", f"trigger search by keyboard search button ratio at ({x},{y})")
        adb_tap(serial, x, y)
        wait(1 if profile.get("_fast") else 2)
        ensure_clean_page(driver, serial, profile, logger, city, "search.submit.keyboard")
        validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
        if validation.ok:
            return
    except Exception as exc:
        logger.log(city, "search.submit", f"keyboard search tap failed: {exc}")

    logger.log(city, "search.submit", "trigger search by ENTER")
    adb_keyevent(serial, 66)
    wait(1 if profile.get("_fast") else 2)
    ensure_clean_page(driver, serial, profile, logger, city, "search.submit.enter")
    validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
    if validation.ok:
        return

    if click_xpath_if_exists(driver, "//*[@text='搜索']"):
        logger.log(city, "search.submit", "trigger search by page 搜索 button")
        wait(1 if profile.get("_fast") else 2)
        ensure_clean_page(driver, serial, profile, logger, city, "search.submit.button")
        validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
        if validation.ok:
            return

    texts = unique_texts(driver)
    if brand.search_keyword in texts and click_text_or_desc(driver, brand.search_keyword):
        logger.log(city, "search.submit", "trigger search by keyword suggestion")
        wait(1 if profile.get("_fast") else 2)
        ensure_clean_page(driver, serial, profile, logger, city, "search.submit.suggestion")
        validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
        if validation.ok:
            return

    raise CaptureFailure("search_failed", f"搜索动作后未进入正式结果页: {validation.reason}")


def recommendation_hits(driver: webdriver.Remote, profile: dict[str, Any]) -> list[str]:
    blob = page_text_blob(driver)
    cfg = screenshot_config(profile)
    return [token for token in cfg["recommendation_texts_as_warning"] if token in blob]


def return_to_result_top_if_needed(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    brand: BrandConfig,
    logger: RunLogger,
    city: str,
) -> None:
    cfg = screenshot_config(profile)
    if not cfg["force_top_before_capture"]:
        return
    hits = recommendation_hits(driver, profile)
    if not hits:
        return

    logger.log(city, "capture.prepare", f"recommendation warning before screenshot: {hits}")
    width = int(profile["screen_width"])
    height = int(profile["screen_height"])
    x = width // 2
    start_y = int(height * 0.34)
    end_y = int(height * 0.84)
    for step in range(2):
        adb_run(
            serial,
            [
                "shell",
                "input",
                "swipe",
                str(x),
                str(start_y),
                str(x),
                str(end_y),
                "240",
            ],
        )
        logger.log(city, "capture.prepare", f"swipe back toward result top step={step + 1}")
        wait(0.6 if profile.get("_fast") else 1.0)
        hits = recommendation_hits(driver, profile)
        validation = validate_result_page(driver, profile, brand, logger, city, strict=True)
        if validation.ok and not hits:
            logger.log(city, "capture.prepare", "result top restored; recommendation warning cleared")
            return

    logger.log(city, "capture.prepare", f"recommendation warning still visible after top restore: {hits or 'none'}")


def prepare_result_page_for_capture(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    brand: BrandConfig,
    logger: RunLogger,
    city: str,
    strict: bool,
) -> ValidationResult:
    validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
    if not validation.ok:
        return validation

    return_to_result_top_if_needed(driver, serial, profile, brand, logger, city)
    validation = wait_for_result_page_stable(driver, profile, brand, logger, city, strict)
    if not validation.ok:
        return validation

    cfg = screenshot_config(profile)
    hits = recommendation_hits(driver, profile)
    if hits:
        logger.log(city, "capture.prepare", f"recommendation text present as warning only: {hits}")
    return validation


def capture_viewport(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
) -> ScreenshotArtifact:
    city_dir = Path(tempfile.mkdtemp(prefix="pupu_viewport_"))
    shot_name = f"{date_str}_{city.city}_{brand.brand}_搜索列表_首屏.png"
    shot_path = city_dir / shot_name
    adb_screencap(serial, shot_path)
    image_width, image_height = image_size(shot_path)
    viewport_width = int(profile["screen_width"])
    viewport_height = int(profile["screen_height"])
    warning = ""
    is_longshot = image_height > viewport_height * 1.2
    if image_height <= viewport_height * 1.2:
        warning = "page_may_have_only_one_screen"
    return ScreenshotArtifact(
        path=shot_path,
        cleanup_dir=city_dir,
        metadata={
            "screenshot_mode": "viewport",
            "is_longshot": str(is_longshot).lower(),
            "shot_count": "1",
            "max_shots": "1",
            "viewport_width": str(viewport_width),
            "viewport_height": str(viewport_height),
            "image_width": str(image_width),
            "image_height": str(image_height),
            "crop_applied": "false",
            "crop_reason": "",
            "crop_y": "",
            "stop_reason": "",
            "reached_page_bottom": "false",
            "reached_recommendation_section": "false",
            "maybe_truncated": "false",
            "recommendation_detect_method": "",
            "recommendation_detect_text": "",
            "recommendation_detect_confidence": "",
            "recommendation_detect_shot_index": "",
            "bottom_detection_method": "",
            "warning": warning,
        },
    )


def capture_longshot(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
) -> ScreenshotArtifact:
    city_dir = Path(tempfile.mkdtemp(prefix="pupu_longshot_"))
    longshot_name = f"{date_str}_{city.city}_{brand.brand}_搜索列表_长图.png"
    cfg = profile["longshot"]
    screenshot_cfg = screenshot_config(profile)

    cmd = [
        "python3",
        str(BASE_DIR / "tools" / "capture_long_results_real_device.py"),
        "--serial",
        serial,
        "--output-dir",
        str(city_dir),
        "--output-name",
        longshot_name,
        "--max-shots",
        str(cfg["max_shots"]),
        "--pause-sec",
        str(cfg["pause_sec"]),
        "--swipe-x",
        str(cfg["swipe_x"]),
        "--swipe-start-y",
        str(cfg["swipe_start_y"]),
        "--swipe-end-y",
        str(cfg["swipe_end_y"]),
        "--swipe-duration-ms",
        str(cfg["swipe_duration_ms"]),
        "--content-top",
        str(cfg["content_top"]),
        "--content-bottom",
        str(cfg["content_bottom"]),
        "--min-overlap",
        str(cfg["min_overlap"]),
        "--max-overlap",
        str(cfg["max_overlap"]),
        "--overlap-step",
        str(cfg["overlap_step"]),
        "--identity-threshold",
        str(cfg["identity_threshold"]),
        "--bottom-stable-rounds",
        str(screenshot_cfg["longshot_bottom_stable_rounds"]),
        "--stop-crop-margin",
        str(screenshot_cfg["longshot_stop_crop_margin"]),
        "--min-stop-crop-height",
        str(screenshot_cfg["longshot_min_stop_crop_height"]),
    ]
    for stop_text in screenshot_cfg["longshot_stop_texts"]:
        cmd.extend(["--stop-text", str(stop_text)])
    for bottom_text in screenshot_cfg["longshot_bottom_texts"]:
        cmd.extend(["--bottom-text", str(bottom_text)])
    cmd.extend(["--ocr-min-confidence", str(screenshot_cfg.get("local_ocr_min_confidence", 0.75))])
    template_dir = BASE_DIR / str(screenshot_cfg.get("template_dir", "assets/templates"))
    recommendation_template_dir = template_dir / "recommendation"
    cmd.extend(["--template-dir", str(recommendation_template_dir)])
    cmd.extend(["--template-min-confidence", str(screenshot_cfg.get("template_min_confidence", 0.8))])
    stop_mode = str(screenshot_cfg.get("recommendation_stop_mode", "crop_above"))
    if not screenshot_cfg.get("crop_recommendation_section", True):
        stop_mode = "keep_current_viewport"
    cmd.extend(["--recommendation-stop-mode", stop_mode])
    if profile.get("_debug_recommendation", False):
        debug_dir = (
            BASE_DIR
            / "debug"
            / "recommendation"
            / date_str
            / brand.brand
            / city.city
        )
        if debug_dir.exists():
            shutil.rmtree(debug_dir)
        cmd.append("--debug-recommendation")
        cmd.extend(["--debug-dir", str(debug_dir)])
    subprocess.run(cmd, check=True)
    shot_path = city_dir / longshot_name
    metadata_path = city_dir / f"{Path(longshot_name).stem}_segments.json"
    metadata = longshot_metadata(shot_path, metadata_path, profile, cfg, brand)
    return ScreenshotArtifact(path=shot_path, cleanup_dir=city_dir, metadata=metadata)


def capture_pages(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
) -> ScreenshotArtifact:
    city_dir = Path(tempfile.mkdtemp(prefix="pupu_pages_"))
    metadata_name = f"{date_str}_{city.city}_{brand.brand}_分页截图.png"
    cfg = profile["longshot"]
    screenshot_root = profile.get("screenshot", {}) if isinstance(profile.get("screenshot"), dict) else {}
    pages_cfg = dict(screenshot_root.get("h5" if profile.get("_h5_only", False) else "pages", {}))
    screenshot_cfg = screenshot_config(profile)
    scroll_ratio = float(pages_cfg.get("scroll_ratio", 0.6))
    max_pages = int(pages_cfg.get("max_pages", cfg.get("max_shots", 20)))
    content_top = int(cfg["content_top"])
    content_bottom = int(cfg["content_bottom"])
    swipe_start_y = int(cfg["swipe_start_y"])
    scroll_distance = max(1, int((content_bottom - content_top) * scroll_ratio))
    swipe_end_y = max(content_top, swipe_start_y - scroll_distance)

    cmd = [
        "python3",
        str(BASE_DIR / "tools" / "capture_long_results_real_device.py"),
        "--serial",
        serial,
        "--output-dir",
        str(city_dir),
        "--output-name",
        metadata_name,
        "--output-mode",
        "pages",
        "--max-shots",
        str(max_pages),
        "--pause-sec",
        str(cfg["pause_sec"]),
        "--swipe-x",
        str(cfg["swipe_x"]),
        "--swipe-start-y",
        str(swipe_start_y),
        "--swipe-end-y",
        str(swipe_end_y),
        "--swipe-duration-ms",
        str(cfg["swipe_duration_ms"]),
        "--content-top",
        str(content_top),
        "--content-bottom",
        str(content_bottom),
        "--min-overlap",
        str(cfg["min_overlap"]),
        "--max-overlap",
        str(cfg["max_overlap"]),
        "--overlap-step",
        str(cfg["overlap_step"]),
        "--identity-threshold",
        str(cfg["identity_threshold"]),
        "--bottom-stable-rounds",
        str(screenshot_cfg["longshot_bottom_stable_rounds"]),
        "--stop-crop-margin",
        str(screenshot_cfg["longshot_stop_crop_margin"]),
        "--min-stop-crop-height",
        str(screenshot_cfg["longshot_min_stop_crop_height"]),
    ]
    for stop_text in screenshot_cfg["longshot_stop_texts"]:
        cmd.extend(["--stop-text", str(stop_text)])
    for bottom_text in screenshot_cfg["longshot_bottom_texts"]:
        cmd.extend(["--bottom-text", str(bottom_text)])
    cmd.extend(["--ocr-min-confidence", str(screenshot_cfg.get("local_ocr_min_confidence", 0.75))])
    template_dir = BASE_DIR / str(screenshot_cfg.get("template_dir", "assets/templates"))
    recommendation_template_dir = template_dir / "recommendation"
    cmd.extend(["--template-dir", str(recommendation_template_dir)])
    cmd.extend(["--template-min-confidence", str(screenshot_cfg.get("template_min_confidence", 0.8))])
    cmd.extend(["--recommendation-stop-mode", "keep_current_viewport"])
    if profile.get("_debug_recommendation", False):
        debug_dir = BASE_DIR / "debug" / "recommendation" / date_str / brand.brand / city.city
        if debug_dir.exists():
            shutil.rmtree(debug_dir)
        cmd.append("--debug-recommendation")
        cmd.extend(["--debug-dir", str(debug_dir)])
    subprocess.run(cmd, check=True)
    metadata_path = city_dir / f"{Path(metadata_name).stem}_segments.json"
    metadata = pages_metadata(city_dir, metadata_path, profile, cfg)
    metadata["screenshot_output_mode"] = "h5" if profile.get("_h5_only", False) else "pages"
    metadata["scroll_ratio"] = f"{scroll_ratio:.2f}"
    metadata["overlap_ratio"] = f"{float(pages_cfg.get('overlap_ratio', max(0.0, 1.0 - scroll_ratio))):.2f}"
    return ScreenshotArtifact(path=city_dir, cleanup_dir=city_dir, metadata=metadata)


def adb_shell_text(serial: str | None, args: list[str]) -> str:
    result = subprocess.run(adb_prefix(serial) + ["shell"] + args, text=True, capture_output=True)
    return result.stdout or ""


def list_device_screenshots(serial: str | None) -> dict[str, int]:
    roots = [
        "/sdcard/DCIM/Screenshots",
        "/sdcard/Pictures/Screenshots",
        "/sdcard/DCIM/Camera",
    ]
    found: dict[str, int] = {}
    for root in roots:
        output = adb_shell_text(
            serial,
            [
                "find",
                root,
                "-maxdepth",
                "1",
                "-type",
                "f",
                "\\(",
                "-iname",
                "*.png",
                "-o",
                "-iname",
                "*.jpg",
                "-o",
                "-iname",
                "*.jpeg",
                "\\)",
                "-printf",
                "%T@ %p\n",
            ],
        )
        for line in output.splitlines():
            parts = line.strip().split(" ", 1)
            if len(parts) != 2:
                continue
            try:
                found[parts[1]] = int(float(parts[0]))
            except ValueError:
                continue
    return found


def pull_device_file(serial: str | None, remote_path: str, local_path: Path) -> None:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(adb_prefix(serial) + ["pull", remote_path, str(local_path)], check=True, text=True, capture_output=True)


def tap_screenshot_overlay_button(
    serial: str | None,
    profile: dict[str, Any],
    logger: RunLogger,
    city: str,
) -> tuple[bool, str]:
    native_cfg = profile.get("native_longshot", {}) if isinstance(profile.get("native_longshot"), dict) else {}
    labels = [str(item) for item in native_cfg.get("longshot_button_texts", []) if item] or [
        "截长屏",
        "长截图",
        "滚动截图",
        "长截屏",
        "Scroll",
        "scroll",
    ]
    dump_path = "/sdcard/pupu_native_longshot_window.xml"
    scan_rounds = int(native_cfg.get("overlay_scan_rounds", 6))
    for round_index in range(scan_rounds):
        subprocess.run(adb_prefix(serial) + ["shell", "uiautomator", "dump", dump_path], text=True, capture_output=True)
        xml = adb_shell_text(serial, ["cat", dump_path])
        for label in labels:
            match = re.search(
                rf'(?:text|content-desc)="[^"]*{re.escape(label)}[^"]*".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                xml,
            )
            if not match:
                continue
            x1, y1, x2, y2 = [int(item) for item in match.groups()]
            x = (x1 + x2) // 2
            y = (y1 + y2) // 2
            logger.log(city, "native-longshot", f"tap system longshot button label={label} at ({x},{y})")
            adb_tap(serial, x, y)
            return True, label
        wait(0.8)
        logger.log(city, "native-longshot", f"system screenshot overlay scan round={round_index + 1}, button=not_found")
    points = native_cfg.get("fallback_longshot_points", [])
    if isinstance(points, list):
        screen_width = int(profile.get("screen_width", 1080))
        screen_height = int(profile.get("screen_height", 2248))
        for point in points:
            if not isinstance(point, dict):
                continue
            name = str(point.get("name", "fallback_longshot_point"))
            x = int(screen_width * float(point.get("x_ratio", 0.5)))
            y = int(screen_height * float(point.get("y_ratio", 0.88)))
            logger.log(city, "native-longshot", f"tap fallback longshot point={name} at ({x},{y})")
            adb_tap(serial, x, y)
            wait(1.0)
            return True, name
    return False, ""


def capture_native_longshot(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
) -> ScreenshotArtifact:
    before = list_device_screenshots(serial)
    logger = RunLogger(date_str)
    logger.log(city.city, "native-longshot", "trigger system screenshot by KEYCODE_SYSRQ")
    adb_keyevent(serial, 120)
    wait(1.2)
    clicked, method = tap_screenshot_overlay_button(serial, profile, logger, city.city)
    if not clicked:
        tmp_dir = Path(tempfile.mkdtemp(prefix="pupu_native_longshot_failed_"))
        failed_marker = tmp_dir / "native_longshot_unavailable.png"
        adb_screencap(serial, failed_marker)
        return ScreenshotArtifact(
            path=failed_marker,
            cleanup_dir=tmp_dir,
            metadata={
                "screenshot_output_mode": "native-longshot",
                "system_longshot_supported": "false",
                "native_longshot_supported": "false",
                "system_longshot_trigger_method": "keyevent_120",
                "native_longshot_image_path": "",
                "system_longshot_raw_path": "",
                "h5_image_path": "",
                "image_width": "",
                "image_height": "",
                "reached_recommendation_section": "false",
                "recommendation_detect_method": "",
                "recommendation_detect_text": "",
                "recommendation_detect_confidence": "",
                "crop_applied": "false",
                "crop_reason": "",
                "maybe_truncated": "true",
                "warning": "system_longshot_button_not_found",
                "native_failure_reason": "system longshot button not found after screenshot",
            },
        )

    native_cfg = profile.get("native_longshot", {}) if isinstance(profile.get("native_longshot"), dict) else {}
    wait(float(native_cfg.get("wait_after_start_seconds", 12)))
    after = list_device_screenshots(serial)
    new_items = {path: ts for path, ts in after.items() if path not in before or ts > before.get(path, 0)}
    if not new_items:
        tmp_dir = Path(tempfile.mkdtemp(prefix="pupu_native_longshot_failed_"))
        failed_marker = tmp_dir / "native_longshot_no_file.png"
        adb_screencap(serial, failed_marker)
        return ScreenshotArtifact(
            path=failed_marker,
            cleanup_dir=tmp_dir,
            metadata={
                "screenshot_output_mode": "native-longshot",
                "system_longshot_supported": "false",
                "native_longshot_supported": "false",
                "system_longshot_trigger_method": f"keyevent_120+{method}",
                "native_longshot_image_path": "",
                "system_longshot_raw_path": "",
                "h5_image_path": "",
                "image_width": "",
                "image_height": "",
                "reached_recommendation_section": "false",
                "recommendation_detect_method": "",
                "recommendation_detect_text": "",
                "recommendation_detect_confidence": "",
                "crop_applied": "false",
                "crop_reason": "",
                "maybe_truncated": "true",
                "warning": "system_longshot_file_not_found",
                "native_failure_reason": "no new screenshot file found on device",
            },
        )

    remote_path = max(new_items.items(), key=lambda item: item[1])[0]
    tmp_dir = Path(tempfile.mkdtemp(prefix="pupu_native_longshot_"))
    local_raw = tmp_dir / "native_system_raw.png"
    pull_device_file(serial, remote_path, local_raw)
    image_width, image_height = image_size(local_raw)
    viewport_height = int(profile["screen_height"])
    maybe_truncated = image_height <= int(viewport_height * 1.2)
    return ScreenshotArtifact(
        path=local_raw,
        cleanup_dir=tmp_dir,
        metadata={
            "screenshot_output_mode": "native-longshot",
            "screenshot_mode": "native-longshot",
            "system_longshot_supported": str(not maybe_truncated).lower(),
            "native_longshot_supported": str(not maybe_truncated).lower(),
            "system_longshot_trigger_method": f"keyevent_120+{method}",
            "native_device_path": remote_path,
            "image_width": str(image_width),
            "image_height": str(image_height),
            "viewport_width": str(profile["screen_width"]),
            "viewport_height": str(viewport_height),
            "reached_recommendation_section": "false",
            "recommendation_detect_method": "",
            "recommendation_detect_text": "",
            "recommendation_detect_confidence": "",
            "crop_applied": "false",
            "crop_reason": "",
            "maybe_truncated": str(maybe_truncated).lower(),
            "warning": "system_longshot_incomplete_or_first_screen_only" if maybe_truncated else "",
        },
    )


def capture_results_screenshot(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
) -> ScreenshotArtifact:
    if profile.get("_native_longshot", False):
        return capture_native_longshot(serial, city, brand, profile, date_str)
    if profile.get("_pages_screenshot", False):
        return capture_pages(serial, city, brand, profile, date_str)
    if profile.get("_scroll_screenshot", False):
        return capture_longshot(serial, city, brand, profile, date_str)
    return capture_viewport(serial, city, brand, profile, date_str)


def image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.width, img.height


def candidate_crop_is_safe(
    crop_y: int,
    image_height: int,
    viewport_height: int,
    evidence_text: str,
    brand: BrandConfig,
    min_safe_ratio: float,
) -> bool:
    if crop_y <= int(viewport_height * min_safe_ratio):
        return False
    if crop_y < int(image_height * 0.25):
        return False
    if brand.brand in evidence_text or brand.search_keyword in evidence_text:
        return True
    return bool(re.search(r"[¥￥]\s*\d|(?:^|[^\d])\d{1,3}\.\d{1,2}(?:[^\d]|$)", evidence_text))


def crop_image_at(path: Path, crop_y: int) -> tuple[int, int]:
    with Image.open(path) as img:
        width, height = img.size
        safe_y = max(1, min(crop_y, height))
        img.crop((0, 0, width, safe_y)).save(path)
        return width, safe_y


def normalize_ocr_box_y(box: Any) -> int:
    try:
        return int(min(float(point[1]) for point in box))
    except Exception:
        return 0


def detect_recommendation_by_local_ocr(
    shot_path: Path,
    brand: BrandConfig,
    profile: dict[str, Any],
) -> RecommendationDetection:
    screenshot_cfg = screenshot_config(profile)
    if not screenshot_cfg["local_ocr_enabled"]:
        return RecommendationDetection(False, warning="local_ocr_disabled")
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:
        return RecommendationDetection(False, warning="local_ocr_unavailable")

    recommendation_texts = screenshot_cfg["recommendation_texts_as_warning"]
    min_confidence = screenshot_cfg["local_ocr_min_confidence"]
    chunk_height = screenshot_cfg["local_ocr_chunk_height"]
    chunk_overlap = screenshot_cfg["local_ocr_chunk_overlap"]
    viewport_height = int(profile["screen_height"])
    min_safe_ratio = screenshot_cfg["min_safe_crop_height_ratio"]

    ocr = RapidOCR()
    evidence_lines: list[str] = []
    best: RecommendationDetection | None = None
    with Image.open(shot_path) as img:
        image_width, image_height = img.size
        step = max(1, chunk_height - chunk_overlap)
        for top in range(0, image_height, step):
            bottom = min(image_height, top + chunk_height)
            chunk = img.crop((0, top, image_width, bottom)).convert("RGB")
            try:
                result, _ = ocr(np.asarray(chunk))
            except Exception as exc:
                return RecommendationDetection(False, warning=f"local_ocr_failed:{exc}")
            for item in result or []:
                if not isinstance(item, list) or len(item) < 3:
                    continue
                box, text, confidence = item[0], str(item[1]), float(item[2])
                y = top + normalize_ocr_box_y(box)
                if y < image_height:
                    evidence_lines.append(text)
                matched = [token for token in recommendation_texts if token and token in text]
                if not matched or confidence < min_confidence:
                    continue
                evidence_text = "\n".join(line for line in evidence_lines if line != text or y > 0)
                if not candidate_crop_is_safe(y, image_height, viewport_height, evidence_text, brand, min_safe_ratio):
                    warning = "crop_position_unsafe;saved_full_longshot"
                    candidate = RecommendationDetection(
                        True,
                        method="ocr",
                        text=",".join(matched),
                        confidence=confidence,
                        crop_y=y,
                        warning=warning,
                    )
                    best = candidate if best is None or y < best.crop_y else best
                    continue
                candidate = RecommendationDetection(
                    True,
                    method="ocr",
                    text=",".join(matched),
                    confidence=confidence,
                    crop_y=y,
                )
                best = candidate if best is None or y < best.crop_y else best
            if best and not best.warning:
                break
    return best or RecommendationDetection(False, method="ocr", warning="recommendation_text_not_detected_by_ocr")


def detect_recommendation_by_template(
    shot_path: Path,
    brand: BrandConfig,
    profile: dict[str, Any],
) -> RecommendationDetection:
    screenshot_cfg = screenshot_config(profile)
    if not screenshot_cfg["template_enabled"]:
        return RecommendationDetection(False, warning="template_matching_disabled")
    try:
        import cv2  # type: ignore
    except Exception:
        return RecommendationDetection(False, warning="opencv_unavailable")

    template_dir = BASE_DIR / screenshot_cfg["template_dir"]
    templates = sorted(template_dir.glob("*.png")) if template_dir.exists() else []
    if not templates:
        return RecommendationDetection(False, method="template", warning="template_not_configured")

    min_confidence = screenshot_cfg["template_min_confidence"]
    viewport_height = int(profile["screen_height"])
    min_safe_ratio = screenshot_cfg["min_safe_crop_height_ratio"]
    image = cv2.imread(str(shot_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return RecommendationDetection(False, warning="template_image_read_failed")
    image_height, _ = image.shape

    best: RecommendationDetection | None = None
    for template_path in templates:
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None:
            continue
        if template.shape[0] > image.shape[0] or template.shape[1] > image.shape[1]:
            continue
        result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        _, max_value, _, max_loc = cv2.minMaxLoc(result)
        if max_value < min_confidence:
            continue
        crop_y = int(max_loc[1])
        # Template detection has no text evidence; only accept conservative lower-page hits.
        if not candidate_crop_is_safe(
            crop_y,
            image_height,
            viewport_height,
            brand.brand,
            brand,
            max(min_safe_ratio, 1.8),
        ):
            candidate = RecommendationDetection(
                True,
                method="template",
                text=template_path.stem,
                confidence=float(max_value),
                crop_y=crop_y,
                warning="crop_position_unsafe;saved_full_longshot",
            )
            best = candidate if best is None or crop_y < best.crop_y else best
            continue
        candidate = RecommendationDetection(
            True,
            method="template",
            text=template_path.stem,
            confidence=float(max_value),
            crop_y=crop_y,
        )
        best = candidate if best is None or crop_y < best.crop_y else best
    return best or RecommendationDetection(False, method="template", warning="recommendation_template_not_detected")


def validate_page_transitions(page_paths: list[Path], enabled: bool) -> str:
    if not enabled or len(page_paths) < 2:
        return ""
    warnings: list[str] = []
    try:
        import cv2  # type: ignore
    except Exception:
        return "transition_validation_unavailable"
    for index in range(1, len(page_paths)):
        try:
            with Image.open(page_paths[index - 1]) as prev_img, Image.open(page_paths[index]) as curr_img:
                prev = prev_img.convert("L")
                curr = curr_img.convert("L")
                width = min(prev.width, curr.width)
                x1 = int(width * 0.08)
                x2 = int(width * 0.92)
                prev_band = prev.crop((x1, int(prev.height * 0.55), x2, int(prev.height * 0.88)))
                curr_band = curr.crop((x1, int(curr.height * 0.08), x2, int(curr.height * 0.72)))
                template = np.asarray(prev_band, dtype=np.uint8)
                search = np.asarray(curr_band, dtype=np.uint8)
                if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
                    warnings.append(f"transition_{index}_{index+1}_overlap_unverified")
                    continue
                result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
                _, max_value, _, _ = cv2.minMaxLoc(result)
                if max_value >= 0.40:
                    warnings.append(f"transition_{index}_{index+1}_overlap_verified")
                else:
                    warnings.append(f"transition_{index}_{index+1}_overlap_unverified:{max_value:.3f}")
        except Exception as exc:
            warnings.append(f"transition_{index}_{index+1}_overlap_unverified:{exc}")
    return ";".join(warnings)


def pages_metadata(
    pages_dir: Path,
    metadata_path: Path,
    profile: dict[str, Any],
    cfg: dict[str, Any],
) -> dict[str, str]:
    viewport_width = int(profile["screen_width"])
    viewport_height = int(profile["screen_height"])
    raw_meta: dict[str, Any] = {}
    segments: list[dict[str, Any]] = []
    if metadata_path.exists():
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw_meta = raw
            raw_segments = raw.get("segments")
            if isinstance(raw_segments, list):
                segments = [item for item in raw_segments if isinstance(item, dict)]
    page_paths = sorted(pages_dir.glob("segment_*.png"))
    screenshot_root = profile.get("screenshot", {}) if isinstance(profile.get("screenshot"), dict) else {}
    pages_cfg = dict(screenshot_root.get("h5" if profile.get("_h5_only", False) else "pages", {}))
    page_count = int(raw_meta.get("shot_count") or len(page_paths))
    max_pages = int(raw_meta.get("max_shots") or cfg.get("max_shots", page_count))
    first_width = ""
    first_height = ""
    if page_paths:
        width, height = image_size(page_paths[0])
        first_width = str(width)
        first_height = str(height)
    capture_stop_reason = str(raw_meta.get("capture_stop_reason", ""))
    reached_page_bottom = bool(raw_meta.get("reached_page_bottom", False))
    reached_recommendation_section = bool(raw_meta.get("reached_recommendation_section", False))
    maybe_truncated = False
    warnings: list[str] = [str(item) for item in raw_meta.get("recommendation_warnings", []) if item]
    if reached_recommendation_section:
        warnings.append("recommendation_detected_last_viewport_kept")
    if capture_stop_reason == "max_shots_reached" and not reached_page_bottom and not reached_recommendation_section:
        maybe_truncated = True
        warnings.append("max_pages_reached;maybe_truncated")
    transition_enabled = bool(pages_cfg.get("transition_validation", False))
    transition_warnings = validate_page_transitions(page_paths, transition_enabled)
    if transition_warnings:
        warnings.append(transition_warnings)
    return {
        "screenshot_output_mode": "pages",
        "screenshot_mode": "pages",
        "is_longshot": "false",
        "page_count": str(page_count),
        "shot_count": str(page_count),
        "max_shots": str(max_pages),
        "viewport_width": str(viewport_width),
        "viewport_height": str(viewport_height),
        "image_width": first_width,
        "image_height": first_height,
        "crop_applied": "false",
        "crop_reason": "",
        "crop_y": "",
        "stop_reason": capture_stop_reason,
        "reached_page_bottom": str(reached_page_bottom).lower(),
        "reached_recommendation_section": str(reached_recommendation_section).lower(),
        "maybe_truncated": str(maybe_truncated).lower(),
        "recommendation_detect_method": str(raw_meta.get("recommendation_detect_method", "")),
        "recommendation_detect_text": str(raw_meta.get("recommendation_detect_text", "")),
        "recommendation_detect_confidence": str(raw_meta.get("recommendation_detect_confidence", "")),
        "recommendation_detect_shot_index": str(raw_meta.get("recommendation_detect_shot_index", "")),
        "bottom_detection_method": str(raw_meta.get("bottom_detection_method", "")),
        "transition_validation_enabled": str(transition_enabled).lower(),
        "transition_warnings": transition_warnings,
        "warning": ";".join(warnings),
    }


def longshot_metadata(
    shot_path: Path,
    metadata_path: Path,
    profile: dict[str, Any],
    cfg: dict[str, Any],
    brand: BrandConfig,
) -> dict[str, str]:
    viewport_width = int(profile["screen_width"])
    viewport_height = int(profile["screen_height"])
    image_width, image_height = image_size(shot_path)
    segments: list[dict[str, Any]] = []
    capture_stop_reason = ""
    stop_reason = ""
    reached_page_bottom = False
    reached_recommendation_section = False
    bottom_detection_method = ""
    raw_meta: dict[str, Any] = {}
    metadata_shot_count: int | None = None
    if metadata_path.exists():
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw_meta = raw
            capture_stop_reason = str(raw.get("capture_stop_reason", ""))
            stop_reason = capture_stop_reason
            reached_page_bottom = bool(raw.get("reached_page_bottom", False))
            reached_recommendation_section = bool(raw.get("reached_recommendation_section", False))
            bottom_detection_method = str(raw.get("bottom_detection_method", ""))
            raw_shot_count = raw.get("shot_count")
            if isinstance(raw_shot_count, int):
                metadata_shot_count = raw_shot_count
            raw_segments = raw.get("segments")
            if isinstance(raw_segments, list):
                segments = [item for item in raw_segments if isinstance(item, dict)]
        elif isinstance(raw, list):
            segments = [item for item in raw if isinstance(item, dict)]
    shot_count = metadata_shot_count or (len(segments) if segments else 1)
    max_shots = int(cfg.get("max_shots", shot_count))
    crop_segments = [item for item in segments if item.get("crop_bottom") is not None]
    crop_applied = bool(crop_segments)
    reached_recommendation_section = reached_recommendation_section or crop_applied
    recommendation_detect_method = str(raw_meta.get("recommendation_detect_method", "uiautomator" if crop_applied else ""))
    recommendation_detect_text = str(raw_meta.get("recommendation_detect_text", ""))
    recommendation_detect_confidence = str(raw_meta.get("recommendation_detect_confidence", "1.000" if crop_applied else ""))
    recommendation_detect_shot_index = str(raw_meta.get("recommendation_detect_shot_index", ""))
    crop_y = ""
    maybe_truncated = False
    warnings: list[str] = [str(item) for item in raw_meta.get("recommendation_warnings", []) if item]
    crop_reason = ""
    if crop_applied:
        method_for_reason = recommendation_detect_method or "uiautomator"
        crop_reason = f"recommendation_section_detected_by_{method_for_reason}"
        crop_y = str(image_height)
        if not recommendation_detect_text:
            detected_texts: list[str] = []
            for item in crop_segments:
                for token in item.get("stop_texts") or []:
                    if token not in detected_texts:
                        detected_texts.append(str(token))
            recommendation_detect_text = ",".join(detected_texts)
        warnings.append("recommendation_crop_applied")
    elif reached_recommendation_section:
        crop_reason = ""
        warnings.append("recommendation_detected_last_viewport_kept")
    else:
        warnings.append("recommendation_text_not_detected_current_viewport;saved_full_longshot")
    if shot_count >= max_shots and not crop_applied:
        warnings.append("longshot_max_shots_reached")
        if not reached_page_bottom:
            maybe_truncated = True
            warnings.append("maybe_truncated")
    if image_height <= viewport_height * 1.2:
        warnings.append("page_may_have_only_one_screen" if shot_count <= 1 else "suspected_not_longshot")
    if capture_stop_reason == "max_shots_reached" and not reached_page_bottom:
        maybe_truncated = True
    return {
        "screenshot_mode": "longshot",
        "is_longshot": str(image_height > viewport_height * 1.2).lower(),
        "shot_count": str(shot_count),
        "max_shots": str(max_shots),
        "viewport_width": str(viewport_width),
        "viewport_height": str(viewport_height),
        "image_width": str(image_width),
        "image_height": str(image_height),
        "crop_applied": str(crop_applied).lower(),
        "crop_reason": crop_reason,
        "crop_y": crop_y,
        "stop_reason": stop_reason,
        "reached_page_bottom": str(reached_page_bottom).lower(),
        "reached_recommendation_section": str(reached_recommendation_section).lower(),
        "maybe_truncated": str(maybe_truncated).lower(),
        "recommendation_detect_method": recommendation_detect_method,
        "recommendation_detect_text": recommendation_detect_text,
        "recommendation_detect_confidence": recommendation_detect_confidence,
        "recommendation_detect_shot_index": recommendation_detect_shot_index,
        "bottom_detection_method": bottom_detection_method,
        "warning": ";".join(warnings),
    }


def archive_result_screenshot(source_path: Path, city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    cmd = [
        "python3",
        str(BASE_DIR / "tools" / "organize_daily_brand_screenshot.py"),
        "--source",
        str(source_path),
        "--brand",
        brand.brand,
        "--city",
        city.city_alias,
        "--date",
        date_str,
        "--base-dir",
        str(SCREENSHOT_DIR),
    ]
    result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    return Path(result.stdout.strip())


def archive_pages_screenshot(source_dir: Path, city: CityConfig, brand: BrandConfig, date_str: str) -> Path:
    target_dir = final_pages_dir(city, brand, date_str)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    page_paths = sorted(source_dir.glob("segment_*.png"))
    if not page_paths:
        raise CaptureFailure("screenshot_failed", "no page screenshots captured")
    for index, page_path in enumerate(page_paths, start=1):
        target_path = target_dir / f"{brand.brand}（{city.city_alias} {date_str}）_{index:02d}.png"
        shutil.copy2(page_path, target_path)
    return target_dir


def detect_card_boundary_crop_y(img: Image.Image, fallback: int) -> tuple[int, bool]:
    """Find a safe top crop that starts near the next complete white product card."""
    try:
        import numpy as np
    except Exception:
        return fallback, False
    arr = np.asarray(img.convert("RGB"))
    white_ratio = (
        (arr[:, :, 0] > 248)
        & (arr[:, :, 1] > 248)
        & (arr[:, :, 2] > 248)
    ).mean(axis=1)
    start_y = min(max(320, fallback - 120), max(0, img.height - 1))
    end_y = min(max(start_y + 1, 1400), img.height)
    for y in range(start_y, end_y):
        previous = white_ratio[max(0, y - 12):y].mean() if y > 0 else 1.0
        if white_ratio[y] > 0.75 and previous < 0.45:
            return y, True
    return fallback, False


def adjust_crop_to_card_gap(img: Image.Image, crop_y: int) -> tuple[int, bool, str]:
    """Move a stitch cut to a nearby horizontal gap instead of through product content."""
    try:
        arr = np.asarray(img.convert("RGB"))
    except Exception:
        return crop_y, False, "card_boundary_unavailable"
    height = img.height
    if height <= 0:
        return crop_y, False, "card_boundary_unavailable"
    y_min = max(0, crop_y - 180)
    y_max = min(height - 1, crop_y + 220)
    if y_max <= y_min:
        return crop_y, False, "card_boundary_uncertain_duplicate_kept"
    # Product cards sit on a light gray background; strong near-white or gray rows are safer cut lines.
    row = arr[y_min:y_max]
    white_ratio = (
        (row[:, :, 0] > 246)
        & (row[:, :, 1] > 246)
        & (row[:, :, 2] > 246)
    ).mean(axis=1)
    gray_gap_ratio = (
        (row[:, :, 0] > 232)
        & (row[:, :, 0] < 248)
        & (row[:, :, 1] > 232)
        & (row[:, :, 1] < 248)
        & (row[:, :, 2] > 232)
        & (row[:, :, 2] < 248)
    ).mean(axis=1)
    score = np.maximum(white_ratio, gray_gap_ratio)
    best_local = int(score.argmax())
    best_score = float(score[best_local])
    if best_score < 0.62:
        return crop_y, False, "card_boundary_uncertain_duplicate_kept"
    adjusted = y_min + best_local
    if abs(adjusted - crop_y) <= 12:
        return crop_y, False, ""
    return adjusted, True, "card_boundary_adjusted"


def find_overlap_crop_y(prev_img: Image.Image, curr_img: Image.Image, pages_cfg: dict[str, Any]) -> tuple[int | None, float, str]:
    """Estimate how much of the current viewport is already present in the previous viewport."""
    try:
        import cv2  # type: ignore
    except Exception:
        return None, 0.0, "overlap_removal_unavailable"
    try:
        prev = np.asarray(prev_img.convert("L"), dtype=np.uint8)
        curr = np.asarray(curr_img.convert("L"), dtype=np.uint8)
        height = min(prev.shape[0], curr.shape[0])
        width = min(prev.shape[1], curr.shape[1])
        x1 = int(width * 0.10)
        x2 = int(width * 0.90)
        template_top = int(pages_cfg.get("overlap_template_top", 360))
        template_height = int(pages_cfg.get("overlap_template_height", 760))
        template_top = max(0, min(template_top, height - 80))
        template_bottom = max(template_top + 80, min(height, template_top + template_height))
        search_top = int(height * float(pages_cfg.get("overlap_search_top_ratio", 0.25)))
        search_bottom = int(height * float(pages_cfg.get("overlap_search_bottom_ratio", 0.95)))
        search_top = max(0, min(search_top, height - 80))
        search_bottom = max(search_top + 80, min(height, search_bottom))
        template = curr[template_top:template_bottom, x1:x2]
        search = prev[search_top:search_bottom, x1:x2]
        if template.shape[0] > search.shape[0] or template.shape[1] > search.shape[1]:
            return None, 0.0, "overlap_removal_template_too_large"
        result = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
        _, max_value, _, max_loc = cv2.minMaxLoc(result)
        matched_prev_y = search_top + int(max_loc[1])
        duplicate_height = height - matched_prev_y + template_top
        min_append_height = int(height * float(pages_cfg.get("min_append_height_ratio", 0.18)))
        crop_y = max(0, min(duplicate_height, height - min_append_height))
        return int(crop_y), float(max_value), ""
    except Exception as exc:
        return None, 0.0, f"overlap_removal_failed:{exc}"


def reduce_floating_widgets(img: Image.Image, enabled: bool, page_index: int) -> tuple[Image.Image, str]:
    """Lightly cover repeated edge floaters on subsequent H5 sections without touching the content center."""
    if not enabled or page_index <= 0:
        return img, ""
    cleaned = img.copy()
    try:
        from PIL import ImageDraw
        draw = ImageDraw.Draw(cleaned)
        width, height = cleaned.size
        # Only cover the very narrow right-edge progress floater. The left cart can overlap product
        # images, so keeping it is safer than accidentally masking real SKU evidence.
        draw.rectangle((int(width * 0.965), int(height * 0.16), width, int(height * 0.62)), fill=(245, 247, 250))
        return cleaned, "floating_widget_reduced"
    except Exception:
        cleaned.close()
        return img, "floating_widget_kept"


def create_h5_image(pages_dir: Path, city: CityConfig, brand: BrandConfig, date_str: str, profile: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    page_paths = sorted(pages_dir.glob("*.png"))
    if not page_paths:
        raise CaptureFailure("screenshot_failed", "no page screenshots for h5 image")
    screenshot_root = profile.get("screenshot", {}) if isinstance(profile.get("screenshot"), dict) else {}
    pages_cfg = dict(screenshot_root.get("h5" if profile.get("_h5_only", False) else "pages", {}))
    separator_height = int(pages_cfg.get("h5_separator_height", 44))
    top_crop = int(pages_cfg.get("subsequent_page_top_crop", 0))
    top_crop_mode = str(pages_cfg.get("subsequent_page_top_crop_mode", "fixed"))
    remove_overlap = bool(pages_cfg.get("remove_duplicate_overlap", False))
    overlap_threshold = float(pages_cfg.get("overlap_match_threshold", 0.42))
    reduce_floaters = bool(pages_cfg.get("reduce_floating_widgets", False))
    images = [Image.open(path).convert("RGB") for path in page_paths]
    pieces: list[Image.Image] = []
    card_boundary_adjusted = False
    duplicate_overlap_kept = False
    overlap_removal_applied = False
    duplicate_content_reduced = False
    overlap_warnings: list[str] = []
    card_boundary_warnings: list[str] = []
    floating_widget_statuses: list[str] = []
    for index, img in enumerate(images):
        if index == 0:
            pieces.append(img.copy())
            continue
        crop_y: int | None = None
        if remove_overlap:
            detected_crop_y, confidence, overlap_warning = find_overlap_crop_y(images[index - 1], img, pages_cfg)
            if detected_crop_y is not None and confidence >= overlap_threshold:
                crop_y = detected_crop_y
                overlap_removal_applied = True
                duplicate_content_reduced = True
            else:
                duplicate_overlap_kept = True
                reason = overlap_warning or f"overlap_removal_low_confidence:{confidence:.3f}"
                overlap_warnings.append(f"page_{index+1}_{reason}")
                # We still know capture used high overlap. When exact matching is uncertain,
                # remove only a conservative middle portion of the expected duplicate area.
                fallback_ratio = float(pages_cfg.get("fallback_overlap_crop_ratio", 0.0))
                if fallback_ratio > 0:
                    fallback_crop_y = int(img.height * fallback_ratio)
                    crop_y = max(top_crop, fallback_crop_y)
                    duplicate_content_reduced = True
                    overlap_warnings.append(f"page_{index+1}_fallback_overlap_crop:{crop_y}")
        if crop_y is None:
            crop_y = top_crop
        if top_crop_mode == "card_boundary":
            crop_y, adjusted = detect_card_boundary_crop_y(img, crop_y)
            card_boundary_adjusted = card_boundary_adjusted or adjusted
            adjusted_crop_y, gap_adjusted, boundary_warning = adjust_crop_to_card_gap(img, crop_y)
            if gap_adjusted:
                crop_y = adjusted_crop_y
                card_boundary_adjusted = True
            if boundary_warning:
                card_boundary_warnings.append(f"page_{index+1}_{boundary_warning}")
        safe_top = min(max(0, crop_y), max(0, img.height - 1))
        piece = img.crop((0, safe_top, img.width, img.height))
        cleaned_piece, floating_status = reduce_floating_widgets(piece, reduce_floaters, index)
        if cleaned_piece is not piece:
            piece.close()
        if floating_status:
            floating_widget_statuses.append(floating_status)
        pieces.append(cleaned_piece)
    width = max(piece.width for piece in pieces)
    total_height = sum(piece.height for piece in pieces) + separator_height * max(0, len(pieces) - 1)
    canvas = Image.new("RGB", (width, total_height), "white")
    cursor_y = 0
    for index, piece in enumerate(pieces, start=1):
        canvas.paste(piece, (0, cursor_y))
        cursor_y += piece.height
        if index < len(pieces):
            separator = Image.new("RGB", (width, separator_height), (245, 247, 250))
            canvas.paste(separator, (0, cursor_y))
            cursor_y += separator_height
    target_path = final_h5_path(city, brand, date_str)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target_path)
    for img in images:
        img.close()
    for piece in pieces:
        piece.close()
    floating_widget_handling = "right_edge_floating_widget_reduced;left_cart_kept_for_sku_safety" if "floating_widget_reduced" in floating_widget_statuses else (
        "floating_widget_kept" if floating_widget_statuses else ""
    )
    return target_path, {
        "h5_stitch_mode": "overlap_removed" if remove_overlap else "stacked_viewports",
        "overlap_removal_applied": str(overlap_removal_applied).lower(),
        "overlap_removal_method": str(pages_cfg.get("overlap_removal_method", "opencv_template")) if remove_overlap else "",
        "overlap_removal_warnings": ";".join(overlap_warnings),
        "duplicate_content_reduced": str(duplicate_content_reduced).lower(),
        "floating_widget_handling": floating_widget_handling or ("floating_widget_kept" if len(page_paths) > 1 else ""),
        "card_boundary_adjustment_applied": "true" if card_boundary_adjusted else ("true" if top_crop > 0 and len(page_paths) > 1 else "false"),
        "card_boundary_warnings": ";".join(card_boundary_warnings),
        "duplicate_overlap_kept": str(duplicate_overlap_kept).lower(),
        "final_h5_image_height": str(total_height),
        "original_page_count": str(len(page_paths)),
        "final_section_count": str(len(pieces)),
    }


def archive_native_longshot(source_path: Path, city: CityConfig, brand: BrandConfig, date_str: str) -> tuple[Path, Path]:
    raw_path = final_native_longshot_path(city, brand, date_str)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, raw_path)
    h5_path = final_h5_path(city, brand, date_str)
    h5_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, h5_path)
    return raw_path, h5_path


def cleanup_screenshot_artifact(artifact: ScreenshotArtifact, logger: RunLogger, city: str) -> None:
    if artifact.cleanup_dir is None:
        return
    try:
        shutil.rmtree(artifact.cleanup_dir, ignore_errors=True)
        logger.log(city, "capture.cleanup", f"removed temp screenshot work dir: {artifact.cleanup_dir}")
    except OSError as exc:
        logger.log(city, "capture.cleanup", f"failed to remove temp screenshot work dir: {exc}")


def save_failed_screenshot(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    date_str: str,
    status: str,
    logger: RunLogger,
) -> Path:
    ts = datetime.now(LOCAL_TZ).strftime("%H%M%S")
    failed_dir = FAILED_SCREENSHOT_DIR / brand.brand / date_str / city.city
    failed_dir.mkdir(parents=True, exist_ok=True)
    failed_path = failed_dir / f"{status}_{ts}.png"
    try:
        adb_screencap(serial, failed_path)
        logger.log(city.city, "failed-shot", f"saved failed screenshot: {failed_path}")
    except Exception as exc:
        logger.log(city.city, "failed-shot", f"failed to save screenshot: {exc}")
    return failed_path


def save_failed_artifact(
    artifact: ScreenshotArtifact,
    city: CityConfig,
    brand: BrandConfig,
    date_str: str,
    status: str,
    logger: RunLogger,
) -> Path:
    ts = datetime.now(LOCAL_TZ).strftime("%H%M%S")
    failed_dir = FAILED_SCREENSHOT_DIR / brand.brand / date_str / city.city
    failed_dir.mkdir(parents=True, exist_ok=True)
    failed_path = failed_dir / f"{status}_{ts}.png"
    shutil.copy2(artifact.path, failed_path)
    logger.log(city.city, "failed-shot", f"saved failed artifact: {failed_path}")
    return failed_path


def existing_archive_result(
    city: CityConfig,
    brand: BrandConfig,
    date_str: str,
    logger: RunLogger,
    profile: dict[str, Any],
) -> dict[str, str] | None:
    if profile.get("_pages_screenshot", False):
        archive_path = final_pages_dir(city, brand, date_str)
        exists = archive_path.exists() and any(archive_path.glob("*.png"))
    else:
        archive_path = final_archive_path(city, brand, date_str)
        exists = archive_path.exists()
    if not exists:
        return None
    logger.log(
        city.city,
        "skip",
        f"existing archive found for {brand.brand}, skip without overwrite: {archive_path}",
    )
    return {
        "city": city.city,
        "status": "skipped_existing",
        "archive": str(archive_path),
        "pages_folder_path": str(archive_path) if profile.get("_pages_screenshot", False) else "",
        "h5_image_path": str(final_h5_path(city, brand, date_str)) if profile.get("_h5_pages", False) and final_h5_path(city, brand, date_str).exists() else "",
        "screenshot_output_mode": "h5" if profile.get("_h5_only", False) else ("pages" if profile.get("_pages_screenshot", False) else ("longshot" if profile.get("_scroll_screenshot", False) else "viewport")),
        "native_longshot_supported": "",
        "system_longshot_supported": "",
        "native_longshot_image_path": "",
        "system_longshot_raw_path": "",
        "system_longshot_trigger_method": "",
        "failure_reason": "",
        "skipped": "true",
        "reason": "existing archive, skipped without --overwrite",
        "warning": "existing formal screenshot preserved",
    }


def city_first_mode(profile: dict[str, Any]) -> str:
    return f"city_first_{profile.get('_output_mode', 'viewport')}"


def report_paths(date_str: str, brand: BrandConfig) -> tuple[Path, Path]:
    report_dir = REPORTS_DIR / date_str
    return report_dir / f"{brand.brand}_capture_summary.csv", report_dir / f"{brand.brand}_capture_summary.json"


def load_retry_failed_cities(date_str: str, brand: BrandConfig) -> set[str]:
    _, json_path = report_paths(date_str, brand)
    if not json_path.exists():
        raise SystemExit(f"未找到可用于 --retry-failed 的 summary: {json_path}")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit(f"summary 格式异常: {json_path}")
    return {
        str(item.get("city", ""))
        for item in data
        if isinstance(item, dict)
        and item.get("status") not in {"success", "skipped_existing"}
        and item.get("city")
    }


def load_existing_summary(date_str: str, brand: BrandConfig) -> list[dict[str, str]]:
    _, json_path = report_paths(date_str, brand)
    if not json_path.exists():
        return []
    data = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def merge_retry_summary(
    existing_rows: list[dict[str, str]],
    retry_rows: list[dict[str, str]],
) -> list[dict[str, str]]:
    if not existing_rows:
        return retry_rows
    merged: dict[str, dict[str, str]] = {
        str(row.get("city", "")): row for row in existing_rows if row.get("city")
    }
    for row in retry_rows:
        city = str(row.get("city", ""))
        if city:
            merged[city] = row
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in existing_rows:
        city = str(row.get("city", ""))
        if city and city in merged:
            ordered.append(merged[city])
            seen.add(city)
    for row in retry_rows:
        city = str(row.get("city", ""))
        if city and city not in seen:
            ordered.append(row)
            seen.add(city)
    return ordered


def normalize_summary_row(
    result: dict[str, str],
    brand: BrandConfig,
    date_str: str,
    started_at: str,
    finished_at: str,
    mode: str,
    strict: bool,
    fast: bool,
    overwrite: bool,
    retry_failed: bool,
) -> dict[str, str]:
    start_dt = parse_iso(started_at)
    finish_dt = parse_iso(finished_at)
    step_timings = rounded_timings(result.get("step_timings") if isinstance(result.get("step_timings"), dict) else {})
    slowest_step, slowest_seconds = slowest_step_from_timings(step_timings)
    duration_seconds = (finish_dt - start_dt).total_seconds()
    return {
        "date": date_str,
        "brand": brand.brand,
        "search_keyword": brand.search_keyword,
        "city": result.get("city", ""),
        "status": result.get("status", ""),
        "screenshot_path": result.get("archive", ""),
        "failed_screenshot_path": result.get("failed_screenshot", ""),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": f"{duration_seconds:.1f}",
        "total_duration_seconds": f"{duration_seconds:.1f}",
        "slowest_step": slowest_step,
        "slowest_step_seconds": f"{slowest_seconds:.3f}" if slowest_step else "",
        "step_timings": step_timings,
        "error_message": result.get("reason", ""),
        "warning": result.get("warning", ""),
        "mode": mode,
        "strict": str(strict).lower(),
        "fast": str(fast).lower(),
        "overwrite": str(overwrite).lower(),
        "retry_failed": str(retry_failed).lower(),
        "result_screenshot_path": result.get("result_screenshot", result.get("longshot", "")),
        "longshot_path": result.get("longshot", ""),
        "screenshot_output_mode": result.get("screenshot_output_mode", ""),
        "page_count": result.get("page_count", ""),
        "pages_folder_path": result.get("pages_folder_path", ""),
        "h5_image_path": result.get("h5_image_path", ""),
        "scroll_ratio": result.get("scroll_ratio", ""),
        "overlap_ratio": result.get("overlap_ratio", ""),
        "transition_validation_enabled": result.get("transition_validation_enabled", ""),
        "transition_warnings": result.get("transition_warnings", ""),
        "viewport_count": result.get("viewport_count", result.get("page_count", "")),
        "h5_stitch_mode": result.get("h5_stitch_mode", ""),
        "overlap_removal_applied": result.get("overlap_removal_applied", ""),
        "overlap_removal_method": result.get("overlap_removal_method", ""),
        "overlap_removal_warnings": result.get("overlap_removal_warnings", ""),
        "duplicate_content_reduced": result.get("duplicate_content_reduced", ""),
        "floating_widget_handling": result.get("floating_widget_handling", ""),
        "card_boundary_adjustment_applied": result.get("card_boundary_adjustment_applied", ""),
        "card_boundary_warnings": result.get("card_boundary_warnings", ""),
        "duplicate_overlap_kept": result.get("duplicate_overlap_kept", ""),
        "final_h5_image_height": result.get("final_h5_image_height", ""),
        "original_page_count": result.get("original_page_count", ""),
        "final_section_count": result.get("final_section_count", ""),
        "native_longshot_supported": result.get("native_longshot_supported", result.get("system_longshot_supported", "")),
        "system_longshot_supported": result.get("system_longshot_supported", ""),
        "native_longshot_image_path": result.get("native_longshot_image_path", result.get("system_longshot_raw_path", "")),
        "system_longshot_raw_path": result.get("system_longshot_raw_path", ""),
        "system_longshot_trigger_method": result.get("system_longshot_trigger_method", ""),
        "failure_reason": result.get("native_failure_reason", result.get("reason", "")),
        "screenshot_mode": result.get("screenshot_mode", ""),
        "is_longshot": result.get("is_longshot", ""),
        "shot_count": result.get("shot_count", ""),
        "max_shots": result.get("max_shots", ""),
        "viewport_width": result.get("viewport_width", ""),
        "viewport_height": result.get("viewport_height", ""),
        "image_width": result.get("image_width", ""),
        "image_height": result.get("image_height", ""),
        "crop_applied": result.get("crop_applied", ""),
        "crop_reason": result.get("crop_reason", ""),
        "crop_y": result.get("crop_y", ""),
        "stop_reason": result.get("stop_reason", ""),
        "reached_page_bottom": result.get("reached_page_bottom", ""),
        "reached_recommendation_section": result.get("reached_recommendation_section", ""),
        "maybe_truncated": result.get("maybe_truncated", ""),
        "recommendation_detect_method": result.get("recommendation_detect_method", ""),
        "recommendation_detect_text": result.get("recommendation_detect_text", ""),
        "recommendation_detect_confidence": result.get("recommendation_detect_confidence", ""),
        "recommendation_detect_shot_index": result.get("recommendation_detect_shot_index", ""),
        "bottom_detection_method": result.get("bottom_detection_method", ""),
        "skipped": result.get("skipped", "false"),
    }


def write_summary_reports(rows: list[dict[str, str]], date_str: str, brand: BrandConfig) -> tuple[Path, Path]:
    csv_path, json_path = report_paths(date_str, brand)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "brand",
        "search_keyword",
        "city",
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
        "skipped",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def print_compact_summary(rows: list[dict[str, str]], csv_path: Path, json_path: Path) -> None:
    success = [row for row in rows if row["status"] == "success"]
    skipped = [row for row in rows if row["status"] == "skipped_existing"]
    failed = [row for row in rows if row["status"] not in {"success", "skipped_existing"}]
    total_duration = sum(float(row.get("duration_seconds") or 0) for row in rows)
    print()
    print("Capture summary")
    print(f"- success: {len(success)}")
    print(f"- skipped_existing: {len(skipped)}")
    print(f"- failed: {len(failed)}")
    print(f"- total_duration_seconds: {total_duration:.1f}")
    if failed:
        failed_text = ", ".join(f"{row['city']}({row['status']})" for row in failed)
        print(f"- failed cities: {failed_text}")
    print_top_slow_steps(rows)
    print(f"- csv: {csv_path}")
    print(f"- json: {json_path}")


def row_step_timings(row: dict[str, Any]) -> StepTimings:
    value = row.get("step_timings")
    if isinstance(value, dict):
        return {str(key): float(val) for key, val in value.items()}
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return {str(key): float(val) for key, val in parsed.items()}
        except Exception:
            return {}
    return {}


def print_top_slow_steps(rows: list[dict[str, Any]], limit: int = 5) -> None:
    entries: list[tuple[float, str, str, str]] = []
    for row in rows:
        for step, seconds in row_step_timings(row).items():
            if seconds > 0:
                entries.append((seconds, row.get("city", ""), row.get("brand", ""), step))
    if not entries:
        print("- top slow steps: none")
        return
    entries.sort(reverse=True)
    print("- top slow steps:")
    for seconds, city, brand, step in entries[:limit]:
        print(f"  {city}/{brand}/{step}: {seconds:.3f}s")


def choose_stable_address(
    driver: webdriver.Remote,
    serial: str,
    profile: dict[str, Any],
    city: CityConfig,
    logger: RunLogger,
) -> None:
    max_candidates = int(profile.get("max_address_candidates", 5))
    for index in range(max_candidates):
        ensure_clean_page(driver, serial, profile, logger, city.city, f"address.start_{index+1}")
        if not address_selector_ready(driver):
            open_address_selector(serial, profile, logger, city.city)
        focus_address_input(driver, profile, serial)
        if open_city_list(driver):
            select_city(driver, city.city)
        else:
            logger.log(city.city, "address", "city list entry not visible; search address directly")
        input_keyword(driver, city.address_keyword)
        pick_address_result_by_index(serial, profile, index)
        wait_until(lambda: homepage_ready(driver), 8 if profile.get("_fast") else 10, 0.5)
        ensure_clean_page(driver, serial, profile, logger, city.city, f"address.selected_{index+1}")
        go_home(driver, serial, profile, logger, city.city)
        if homepage_ready(driver):
            logger.log(city.city, "address", f"selected stable address candidate index={index}")
            return
    raise CaptureFailure("city_failed", f"{city.city} 未找到可继续执行搜索的有效点位")


def run_one_capture(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
    appium_server_url: str,
    strict: bool,
    logger: RunLogger,
    overwrite: bool,
) -> dict[str, str]:
    if not overwrite:
        existing = existing_archive_result(city, brand, date_str, logger, profile)
        if existing is not None:
            return existing

    session_cfg = profile.get("session", {})
    max_restarts = int(session_cfg.get("max_restarts", 2)) if isinstance(session_cfg, dict) else 2
    last_error = ""

    for restart_round in range(0, max_restarts + 1):
        driver: webdriver.Remote | None = None
        try:
            logger.log(city.city, "app", "ensure pupu app foreground")
            ensure_app_foreground(serial, profile, logger, city.city)
            driver = connect_driver_with_log(serial, profile, appium_server_url, logger, city.city)
            result = execute_capture_flow(driver, serial, city, brand, profile, date_str, strict, logger)
            return result
        except CaptureFailure as exc:
            failed_path = save_failed_screenshot(serial, city, brand, date_str, exc.status, logger)
            logger.log(city.city, "failure", f"status={exc.status}, reason={exc.reason}")
            return {
                "city": city.city,
                "status": exc.status,
                "reason": exc.reason,
                "failed_screenshot": str(failed_path),
            }
        except (SessionBroken, WebDriverException) as exc:
            last_error = str(exc)
            if not isinstance(exc, SessionBroken) and not is_session_error(exc):
                failed_path = save_failed_screenshot(serial, city, brand, date_str, "app_failed", logger)
                logger.log(city.city, "failure", f"status=app_failed, reason={exc}")
                return {
                    "city": city.city,
                    "status": "app_failed",
                    "reason": str(exc),
                    "failed_screenshot": str(failed_path),
                }
            logger.log(city.city, "session", f"session health check failed: {last_error}")
            safe_driver_quit(driver)
            if restart_round >= max_restarts:
                break
            reset_uiautomator2(serial, logger, city.city, restart_round + 1)
            try:
                ensure_app_foreground(serial, profile, logger, city.city)
                logger.log(city.city, "session", "session restore prepared")
            except Exception as restore_exc:
                logger.log(city.city, "session", f"relaunch after session reset failed: {restore_exc}")
            continue
        except Exception as exc:
            failed_path = save_failed_screenshot(serial, city, brand, date_str, "app_failed", logger)
            logger.log(city.city, "failure", f"status=app_failed, reason={exc}")
            return {
                "city": city.city,
                "status": "app_failed",
                "reason": str(exc),
                "failed_screenshot": str(failed_path),
            }
        finally:
            safe_driver_quit(driver)

    failed_path = save_failed_screenshot(serial, city, brand, date_str, "app_failed", logger)
    logger.log(city.city, "failure", f"status=app_failed, reason=session restore failed: {last_error}")
    return {
        "city": city.city,
        "status": "app_failed",
        "reason": f"session restore failed: {last_error}",
        "failed_screenshot": str(failed_path),
    }


def execute_capture_flow(
    driver: webdriver.Remote,
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
    strict: bool,
    logger: RunLogger,
) -> dict[str, str]:
    try:
        ensure_clean_page(driver, serial, profile, logger, city.city, "app.after_launch")
        go_home(driver, serial, profile, logger, city.city)
        choose_stable_address(driver, serial, profile, city, logger)
        return execute_brand_search_flow(driver, serial, city, brand, profile, date_str, strict, logger)
    except WebDriverException as exc:
        if is_session_error(exc):
            raise SessionBroken(str(exc)) from exc
        raise


def execute_brand_search_flow(
    driver: webdriver.Remote,
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    profile: dict[str, Any],
    date_str: str,
    strict: bool,
    logger: RunLogger,
    base_timings: StepTimings | None = None,
) -> dict[str, str]:
    timings: StepTimings = merge_timings(base_timings)
    total_started = time.perf_counter()
    try:
        logger.log(city.city, "brand", f"start search/capture brand={brand.brand}")
        with timed_step(timings, "dismiss_popups"):
            ensure_clean_page(driver, serial, profile, logger, city.city, "before_search")
        with timed_step(timings, "open_search"):
            open_search(driver, serial, profile, logger, city.city)
        with timed_step(timings, "clear_search_input"):
            clear_search_input(driver, serial, profile, logger, city.city)
        with timed_step(timings, "input_keyword"):
            input_search_keyword(driver, serial, profile, logger, city.city, brand.search_keyword)
        with timed_step(timings, "dismiss_popups"):
            ensure_clean_page(driver, serial, profile, logger, city.city, "after_keyword_input")
        with timed_step(timings, "submit_search"):
            trigger_search(driver, serial, profile, logger, city.city, brand, strict)
        with timed_step(timings, "dismiss_popups"):
            ensure_clean_page(driver, serial, profile, logger, city.city, "before_capture")
        with timed_step(timings, "wait_result_page"):
            validation = prepare_result_page_for_capture(driver, serial, profile, brand, logger, city.city, strict)
        if not validation.ok:
            raise CaptureFailure(validation.status, validation.reason)

        with timed_step(timings, "capture_screenshot"):
            artifact = capture_results_screenshot(serial, city, brand, profile, date_str)
        warnings = [str(artifact.metadata.get("warning", ""))]
        with timed_step(timings, "validate_result_page"):
            warning_hits = recommendation_hits(driver, profile)
        if warning_hits:
            warnings.append(f"recommendation text visible: {', '.join(warning_hits)}")
        warning = ";".join(item for item in warnings if item)

        if (
            artifact.metadata.get("screenshot_mode") == "longshot"
            and artifact.metadata.get("reached_page_bottom") != "true"
            and artifact.metadata.get("reached_recommendation_section") != "true"
        ):
            with timed_step(timings, "save_summary"):
                failed_path = save_failed_artifact(
                    artifact,
                    city,
                    brand,
                    date_str,
                    "incomplete_longshot",
                    logger,
                )
                existing_path = final_archive_path(city, brand, date_str)
                if existing_path.exists():
                    existing_path.unlink()
                    logger.log(city.city, "archive", f"removed stale incomplete official image: {existing_path}")
                cleanup_screenshot_artifact(artifact, logger, city.city)
            add_timing(timings, "total", time.perf_counter() - total_started)
            result = {
                "city": city.city,
                "status": "incomplete_longshot",
                "reason": "longshot did not reach page bottom",
                "failed_screenshot": str(failed_path),
                "warning": warning,
                "step_timings": rounded_timings(timings),
            }
            result.update({key: str(value) for key, value in artifact.metadata.items() if key != "warning"})
            logger.log(city.city, "failure", f"status=incomplete_longshot, failed={failed_path}")
            return result

        if (
            artifact.metadata.get("screenshot_output_mode") in {"pages", "h5"}
            and artifact.metadata.get("maybe_truncated") == "true"
        ):
            with timed_step(timings, "save_summary"):
                failed_path = save_failed_screenshot(serial, city, brand, date_str, "incomplete_pages", logger)
                existing_dir = final_pages_dir(city, brand, date_str)
                if existing_dir.exists():
                    shutil.rmtree(existing_dir)
                    logger.log(city.city, "archive", f"removed stale incomplete official pages: {existing_dir}")
                cleanup_screenshot_artifact(artifact, logger, city.city)
            add_timing(timings, "total", time.perf_counter() - total_started)
            result = {
                "city": city.city,
                "status": "incomplete_pages",
                "reason": "pages capture reached max pages before recommendation or page bottom",
                "failed_screenshot": str(failed_path),
                "warning": warning,
                "step_timings": rounded_timings(timings),
            }
            result.update({key: str(value) for key, value in artifact.metadata.items() if key != "warning"})
            logger.log(city.city, "failure", f"status=incomplete_pages, failed={failed_path}")
            return result

        if artifact.metadata.get("screenshot_output_mode") == "native-longshot":
            if artifact.metadata.get("system_longshot_supported") != "true" or artifact.metadata.get("maybe_truncated") == "true":
                with timed_step(timings, "save_summary"):
                    failed_path = save_failed_screenshot(serial, city, brand, date_str, "system_longshot_unavailable", logger)
                    cleanup_screenshot_artifact(artifact, logger, city.city)
                add_timing(timings, "total", time.perf_counter() - total_started)
                result = {
                    "city": city.city,
                    "status": "system_longshot_unavailable",
                    "reason": artifact.metadata.get("native_failure_reason", artifact.metadata.get("warning", "system longshot unavailable")),
                    "failed_screenshot": str(failed_path),
                    "warning": warning,
                    "step_timings": rounded_timings(timings),
                }
                result.update({key: str(value) for key, value in artifact.metadata.items() if key != "warning"})
                logger.log(city.city, "failure", f"status=system_longshot_unavailable, failed={failed_path}")
                return result

        with timed_step(timings, "save_summary"):
            h5_extra: dict[str, str] = {}
            h5_pages_path = ""
            if artifact.metadata.get("screenshot_output_mode") == "native-longshot":
                native_raw_path, h5_path = archive_native_longshot(artifact.path, city, brand, date_str)
                archive_path = h5_path
                h5_extra = {
                    "native_longshot_image_path": str(native_raw_path),
                    "system_longshot_raw_path": str(native_raw_path),
                    "h5_image_path": str(h5_path),
                }
            elif artifact.metadata.get("screenshot_output_mode") in {"pages", "h5"}:
                if profile.get("_h5_only", False):
                    debug_pages_dir = BASE_DIR / "debug" / "h5_pages" / date_str / brand.brand / city.city
                    if debug_pages_dir.exists():
                        shutil.rmtree(debug_pages_dir)
                    archive_pages_screenshot(artifact.path, city, brand, date_str)
                    pages_for_h5 = final_pages_dir(city, brand, date_str)
                    h5_path, h5_extra = create_h5_image(pages_for_h5, city, brand, date_str, profile)
                    debug_pages_dir.parent.mkdir(parents=True, exist_ok=True)
                    if debug_pages_dir.exists():
                        shutil.rmtree(debug_pages_dir)
                    shutil.copytree(pages_for_h5, debug_pages_dir)
                    shutil.rmtree(pages_for_h5, ignore_errors=True)
                    h5_pages_path = str(debug_pages_dir)
                    archive_path = h5_path
                else:
                    archive_path = archive_pages_screenshot(artifact.path, city, brand, date_str)
                    h5_path, h5_extra = create_h5_image(archive_path, city, brand, date_str, profile) if profile.get("_h5_pages", False) else (None, {})
            else:
                archive_path = archive_result_screenshot(artifact.path, city, brand, date_str)
                h5_path = None
            cleanup_screenshot_artifact(artifact, logger, city.city)
        add_timing(timings, "total", time.perf_counter() - total_started)
        logger.log(city.city, "success", f"archive={archive_path}")
        result = {
            "city": city.city,
            "status": "success",
            "result_screenshot": str(archive_path),
            "longshot": str(archive_path) if profile.get("_scroll_screenshot", False) else "",
            "archive": str(archive_path),
            "pages_folder_path": h5_pages_path if profile.get("_h5_only", False) else (str(archive_path) if artifact.metadata.get("screenshot_output_mode") == "pages" else ""),
            "h5_image_path": str(h5_path) if h5_path else "",
            "warning": warning,
            "step_timings": rounded_timings(timings),
        }
        result.update({key: str(value) for key, value in artifact.metadata.items() if key != "warning"})
        result.update(h5_extra)
        return result
    except WebDriverException as exc:
        if is_session_error(exc):
            raise SessionBroken(str(exc)) from exc
        raise


def city_failure_result(
    serial: str,
    city: CityConfig,
    brand: BrandConfig,
    date_str: str,
    status: str,
    reason: str,
    logger: RunLogger,
    step_timings: StepTimings | None = None,
) -> dict[str, str]:
    failed_path = save_failed_screenshot(serial, city, brand, date_str, status, logger)
    return {
        "city": city.city,
        "status": status,
        "reason": reason,
        "failed_screenshot": str(failed_path),
        "step_timings": rounded_timings(step_timings),
    }


def run_city_first_capture(
    serial: str,
    cities: list[CityConfig],
    brands: list[BrandConfig],
    profile: dict[str, Any],
    date_str: str,
    appium_server_url: str,
    strict: bool,
    logger: RunLogger,
    overwrite: bool,
) -> dict[str, list[dict[str, str]]]:
    rows_by_brand: dict[str, list[dict[str, str]]] = {brand.brand: [] for brand in brands}
    session_cfg = profile.get("session", {})
    max_restarts = int(session_cfg.get("max_restarts", 2)) if isinstance(session_cfg, dict) else 2

    for city in cities:
        pending: list[tuple[BrandConfig, str]] = []
        for brand in brands:
            started_at = now_iso()
            if not overwrite:
                existing = existing_archive_result(city, brand, date_str, logger, profile)
                if existing is not None:
                    finished_at = now_iso()
                    rows_by_brand[brand.brand].append(
                        normalize_summary_row(
                            existing,
                            brand,
                            date_str,
                            started_at,
                            finished_at,
                            city_first_mode(profile),
                            strict,
                            bool(profile.get("_fast", False)),
                            overwrite,
                            False,
                        )
                    )
                    continue
            pending.append((brand, started_at))

        if not pending:
            logger.log(city.city, "city-first", "all tasks already exist, skip city without app actions")
            continue

        last_error = ""
        city_setup_done = False
        for restart_round in range(0, max_restarts + 1):
            driver: webdriver.Remote | None = None
            city_timings: StepTimings = {}
            try:
                logger.log(city.city, "city-first", f"prepare city once for {len(pending)} pending brands")
                with timed_step(city_timings, "ensure_app_foreground"):
                    ensure_app_foreground(serial, profile, logger, city.city)
                with timed_step(city_timings, "appium_session"):
                    driver = connect_driver_with_log(serial, profile, appium_server_url, logger, city.city)
                with timed_step(city_timings, "dismiss_popups"):
                    ensure_clean_page(driver, serial, profile, logger, city.city, "city_first.after_launch")
                with timed_step(city_timings, "switch_city"):
                    go_home(driver, serial, profile, logger, city.city)
                    choose_stable_address(driver, serial, profile, city, logger)
                city_setup_done = True

                for index, (brand, started_at) in enumerate(pending):
                    finished_at = ""
                    try:
                        if index > 0:
                            with timed_step(city_timings, "go_home"):
                                go_home(driver, serial, profile, logger, city.city)
                        result = execute_brand_search_flow(
                            driver,
                            serial,
                            city,
                            brand,
                            profile,
                            date_str,
                            strict,
                            logger,
                            city_timings,
                        )
                    except CaptureFailure as exc:
                        result = city_failure_result(
                            serial,
                            city,
                            brand,
                            date_str,
                            exc.status,
                            exc.reason,
                            logger,
                            city_timings,
                        )
                        logger.log(city.city, "failure", f"brand={brand.brand}, status={exc.status}, reason={exc.reason}")
                    except (SessionBroken, WebDriverException) as exc:
                        last_error = str(exc)
                        if not isinstance(exc, SessionBroken) and not is_session_error(exc):
                            result = city_failure_result(
                                serial,
                                city,
                                brand,
                                date_str,
                                "app_failed",
                                str(exc),
                                logger,
                                city_timings,
                            )
                            logger.log(city.city, "failure", f"brand={brand.brand}, status=app_failed, reason={exc}")
                        else:
                            logger.log(city.city, "session", f"brand={brand.brand}, session broken: {last_error}")
                            result = city_failure_result(
                                serial,
                                city,
                                brand,
                                date_str,
                                "app_failed",
                                f"session broken during city-first brand capture: {last_error}",
                                logger,
                                city_timings,
                            )
                            rows_by_brand[brand.brand].append(
                                normalize_summary_row(
                                    result,
                                    brand,
                                    date_str,
                                    started_at,
                                    now_iso(),
                                    city_first_mode(profile),
                                    strict,
                                    bool(profile.get("_fast", False)),
                                    overwrite,
                                    False,
                                )
                            )
                            remaining = pending[index + 1 :]
                            for remaining_brand, remaining_started_at in remaining:
                                remaining_result = city_failure_result(
                                    serial,
                                    city,
                                    remaining_brand,
                                    date_str,
                                    "app_failed",
                                    f"session broken before brand capture: {last_error}",
                                    logger,
                                    city_timings,
                                )
                                rows_by_brand[remaining_brand.brand].append(
                                    normalize_summary_row(
                                        remaining_result,
                                        remaining_brand,
                                        date_str,
                                        remaining_started_at,
                                        now_iso(),
                                        city_first_mode(profile),
                                        strict,
                                        bool(profile.get("_fast", False)),
                                        overwrite,
                                        False,
                                    )
                            )
                            break
                    except Exception as exc:
                        result = city_failure_result(
                            serial,
                            city,
                            brand,
                            date_str,
                            "app_failed",
                            str(exc),
                            logger,
                            city_timings,
                        )
                        logger.log(city.city, "failure", f"brand={brand.brand}, status=app_failed, reason={exc}")
                    else:
                        logger.log(city.city, "city-first", f"brand={brand.brand}, status={result.get('status')}")

                    if finished_at == "":
                        finished_at = now_iso()
                    if not (
                        result.get("status") == "app_failed"
                        and "session broken" in result.get("reason", "")
                    ):
                        rows_by_brand[brand.brand].append(
                            normalize_summary_row(
                                result,
                                brand,
                                date_str,
                                started_at,
                                finished_at,
                                city_first_mode(profile),
                                strict,
                                bool(profile.get("_fast", False)),
                                overwrite,
                                False,
                            )
                        )
                    if result.get("status") == "app_failed" and "session broken" in result.get("reason", ""):
                        break

                return_to_home_ok = True
                try:
                    with timed_step(city_timings, "go_home"):
                        go_home(driver, serial, profile, logger, city.city)
                except Exception as exc:
                    return_to_home_ok = False
                    logger.log(city.city, "city-first", f"post-city go_home failed but continuing: {exc}")
                logger.log(city.city, "city-first", f"city finished, return_to_home_ok={return_to_home_ok}")
                break
            except CaptureFailure as exc:
                for brand, started_at in pending:
                    result = city_failure_result(serial, city, brand, date_str, exc.status, exc.reason, logger)
                    rows_by_brand[brand.brand].append(
                        normalize_summary_row(
                            result,
                            brand,
                            date_str,
                            started_at,
                            now_iso(),
                            city_first_mode(profile),
                            strict,
                            bool(profile.get("_fast", False)),
                            overwrite,
                            False,
                        )
                    )
                logger.log(city.city, "city-first", f"city setup failed: status={exc.status}, reason={exc.reason}")
                break
            except (SessionBroken, WebDriverException) as exc:
                last_error = str(exc)
                if not isinstance(exc, SessionBroken) and not is_session_error(exc):
                    for brand, started_at in pending:
                        result = city_failure_result(serial, city, brand, date_str, "app_failed", str(exc), logger)
                        rows_by_brand[brand.brand].append(
                            normalize_summary_row(
                                result,
                                brand,
                                date_str,
                                started_at,
                                now_iso(),
                                city_first_mode(profile),
                                strict,
                                bool(profile.get("_fast", False)),
                                overwrite,
                                False,
                            )
                        )
                    break
                logger.log(city.city, "session", f"city-first session failed: {last_error}")
                safe_driver_quit(driver)
                if restart_round >= max_restarts:
                    for brand, started_at in pending:
                        result = city_failure_result(
                            serial,
                            city,
                            brand,
                            date_str,
                            "app_failed",
                            f"session restore failed: {last_error}",
                            logger,
                        )
                        rows_by_brand[brand.brand].append(
                            normalize_summary_row(
                                result,
                                brand,
                                date_str,
                                started_at,
                                now_iso(),
                                city_first_mode(profile),
                                strict,
                                bool(profile.get("_fast", False)),
                                overwrite,
                                False,
                            )
                        )
                    break
                reset_uiautomator2(serial, logger, city.city, restart_round + 1)
                continue
            except Exception as exc:
                for brand, started_at in pending:
                    result = city_failure_result(serial, city, brand, date_str, "app_failed", str(exc), logger)
                    rows_by_brand[brand.brand].append(
                        normalize_summary_row(
                            result,
                            brand,
                            date_str,
                            started_at,
                            now_iso(),
                            city_first_mode(profile),
                            strict,
                            bool(profile.get("_fast", False)),
                            overwrite,
                            False,
                        )
                    )
                logger.log(city.city, "city-first", f"unhandled city error: {exc}")
                break
            finally:
                safe_driver_quit(driver)

        if not city_setup_done and last_error:
            logger.log(city.city, "city-first", f"city ended without setup; last_error={last_error}")

    return rows_by_brand


def main() -> None:
    parser = argparse.ArgumentParser(description="Run real-device Pupu capture.")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--city", help="full city name, e.g. 佛山市")
    parser.add_argument("--brand", help="brand name, e.g. 卫龙")
    parser.add_argument("--all-enabled-brands", action="store_true")
    parser.add_argument("--city-first", action="store_true", help="switch each city once, then capture all selected brands")
    parser.add_argument("--date", default=datetime.now(LOCAL_TZ).strftime("%Y-%m-%d"))
    parser.add_argument("--appium-server-url", default="http://127.0.0.1:4723")
    parser.add_argument("--all-enabled-cities", action="store_true")
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--scroll-screenshot", action="store_true")
    parser.add_argument("--output-mode", choices=["native-longshot", "system-longshot", "h5", "pages", "h5-pages", "viewport", "longshot"], default="")
    parser.add_argument("--legacy-longshot", "--stitch-longshot", action="store_true")
    parser.add_argument("--debug-recommendation", action="store_true")
    args = parser.parse_args()

    if args.all_enabled_brands:
        brands = enabled_brands()
        if not brands:
            raise SystemExit("brands.json 中没有 enabled=true 的品牌。")
    else:
        if not args.brand:
            raise SystemExit("必须提供 --brand，或使用 --all-enabled-brands")
        brands = [pick_brand(args.brand)]

    if args.city_first and args.retry_failed:
        raise SystemExit("--city-first 暂不支持 --retry-failed；请先使用单品牌 --retry-failed。")

    if not args.city_first and len(brands) > 1:
        raise SystemExit("--all-enabled-brands 需要配合 --city-first 使用。")

    brand = brands[0]
    profile = apply_runtime_options(
        load_json(resolve_config_path(REAL_DEVICE_PROFILE_PATH, REAL_DEVICE_PROFILE_LOCAL_PATH)),
        fast=args.fast,
        scroll_screenshot=args.scroll_screenshot,
        debug_recommendation=args.debug_recommendation,
        output_mode=args.output_mode,
        legacy_longshot=args.legacy_longshot,
    )
    capture_mode = str(profile.get("_output_mode", "viewport"))
    logger = RunLogger(args.date)

    if args.all_enabled_cities:
        cities = enabled_cities()
    else:
        if not args.city:
            raise SystemExit("必须提供 --city，或使用 --all-enabled-cities")
        cities = [pick_city(args.city)]

    existing_summary: list[dict[str, str]] = []
    if args.retry_failed:
        existing_summary = load_existing_summary(args.date, brand)
        failed_city_names = load_retry_failed_cities(args.date, brand)
        cities = [city for city in cities if city.city in failed_city_names]
        logger.log("GLOBAL", "retry-failed", f"cities={sorted(failed_city_names) or 'none'}")
        if not cities:
            csv_path, json_path = report_paths(args.date, brand)
            print("No failed cities to retry.")
            print(f"Existing summary remains unchanged: {json_path}")
            return

    if args.dry_run:
        print(
            json.dumps(
                {
                    "cities": [
                        {
                            "city": city.city,
                            "city_alias": city.city_alias,
                            "address_keyword": city.address_keyword,
                        }
                        for city in cities
                    ],
                    "brands": [
                        {
                            "brand": item.brand,
                            "search_keyword": item.search_keyword,
                        }
                        for item in brands
                    ],
                    "brand": brand.brand if len(brands) == 1 else "",
                    "search_keyword": brand.search_keyword if len(brands) == 1 else "",
                    "date": args.date,
                    "strict": args.strict,
                    "fast": args.fast,
                    "overwrite": args.overwrite,
                    "retry_failed": args.retry_failed,
                    "mode": capture_mode,
                    "city_first": args.city_first,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if not appium_status_ready(args.appium_server_url):
        raise SystemExit(f"Appium 未就绪: {args.appium_server_url}")

    if args.city_first:
        rows_by_brand = run_city_first_capture(
            args.serial,
            cities,
            brands,
            profile,
            args.date,
            args.appium_server_url,
            args.strict,
            logger,
            args.overwrite,
        )
        all_rows: list[dict[str, str]] = []
        for brand_item in brands:
            rows = rows_by_brand.get(brand_item.brand, [])
            existing_rows = load_existing_summary(args.date, brand_item)
            output_rows = merge_retry_summary(existing_rows, rows) if existing_rows else rows
            all_rows.extend(output_rows)
            print(json.dumps(output_rows, ensure_ascii=False, indent=2))
            csv_path, json_path = write_summary_reports(output_rows, args.date, brand_item)
            print_compact_summary(output_rows, csv_path, json_path)
        if any(item["status"] not in {"success", "skipped_existing"} for item in all_rows):
            sys.exit(1)
        return

    summary: list[dict[str, str]] = []
    for city in cities:
        last_result: dict[str, str] | None = None
        started_at = now_iso()
        for attempt in range(1, args.retries + 2):
            logger.log(city.city, "attempt", f"start attempt={attempt}")
            try:
                result = run_one_capture(
                    args.serial,
                    city,
                    brand,
                    profile,
                    args.date,
                    args.appium_server_url,
                    args.strict,
                    logger,
                    args.overwrite,
                )
            except Exception as exc:
                logger.log(city.city, "failure", f"unhandled city-level error: {exc}")
                result = {
                    "city": city.city,
                    "status": "app_failed",
                    "reason": str(exc),
                }
            last_result = result
            if result["status"] in {"success", "skipped_existing"}:
                break
            logger.log(city.city, "attempt", f"attempt={attempt} failed with {result['status']}")
            if attempt < args.retries + 1:
                wait(2)
        if last_result is not None:
            finished_at = now_iso()
            summary.append(
                normalize_summary_row(
                    last_result,
                    brand,
                    args.date,
                    started_at,
                    finished_at,
                    capture_mode,
                    args.strict,
                    args.fast,
                    args.overwrite,
                    args.retry_failed,
                )
            )

    output_summary = merge_retry_summary(existing_summary, summary) if args.retry_failed else summary
    print(json.dumps(output_summary, ensure_ascii=False, indent=2))
    csv_path, json_path = write_summary_reports(output_summary, args.date, brand)
    print_compact_summary(output_summary, csv_path, json_path)
    if any(item["status"] not in {"success", "skipped_existing"} for item in output_summary):
        sys.exit(1)


if __name__ == "__main__":
    main()
