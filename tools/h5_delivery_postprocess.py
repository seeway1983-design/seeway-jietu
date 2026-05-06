#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


BASE_DIR = Path(__file__).resolve().parents[1]
DEBUG_H5_PAGES_DIR = BASE_DIR / "debug" / "h5_pages"
DELIVERABLES_H5_DIR = BASE_DIR / "deliverables_h5"
DELIVERABLES_H5_CLEANED_DIR = BASE_DIR / "deliverables_h5_cleaned"
REPORTS_DIR = BASE_DIR / "reports"

PRICE_RE = re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)")
SPEC_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?(?:ml|g|kg|L|包|袋|罐|支|片|\*)(?:[0-9a-zA-Z/*]*)", re.IGNORECASE)


@dataclass
class CardCandidate:
    card_id: str
    page_index: int
    card_index: int
    source_path: str
    bbox: list[int]
    anchor_center: list[int]
    title_text: str
    price_text: str
    spec_text: str
    normalized_title: str
    normalized_spec: str
    image_hash: str
    text_confidence: float
    is_truncated_top: bool
    is_truncated_bottom: bool
    has_title: bool
    has_price: bool
    is_complete: bool
    is_complete_card: bool
    completeness_score: int
    incomplete_reason: list[str]
    has_full_duplicate: bool
    action: str
    action_reason: str
    quality_score: float
    price_source: str
    preview_path: str
    brand_hint_matched: bool
    is_main_result_card: bool
    brand_text_missing: bool
    brand_text_missing_but_main_result_kept: bool
    product_type_hint: str
    is_valid_sku: bool
    exclusion_reason: str
    is_recommendation_zone_card: bool
    warnings: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="离线分析 H5 页图中的重复 SKU 和疑似截断卡片。")
    parser.add_argument("--date", required=True, help="日期，例如 2026-05-04")
    parser.add_argument("--brand", default="", help="品牌，例如 卫龙")
    parser.add_argument("--city", default="", help="城市全名，例如 成都市")
    parser.add_argument("--all", action="store_true", help="全量刷新指定日期的离线分析和 batch summary")
    parser.add_argument(
        "--generate-cleaned",
        action="store_true",
        help="基于当前 batch summary 只复制 delivery_grade=fixable 的 rebuilt 图到 cleaned 目录",
    )
    parser.add_argument(
        "--pages-dir",
        default="",
        help="可选，直接指定页图目录；默认按 debug/h5_pages/日期/品牌/城市 推断",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="可选，直接指定输出 JSON 路径",
    )
    parser.add_argument(
        "--output-csv",
        default="",
        help="可选，直接指定输出 CSV 路径",
    )
    parser.add_argument(
        "--output-md",
        default="",
        help="可选，直接指定输出 Markdown 路径",
    )
    return parser.parse_args()


def normalize_box(box: Any) -> list[list[float]]:
    if not isinstance(box, list):
        return []
    normalized: list[list[float]] = []
    for point in box:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            normalized.append([float(point[0]), float(point[1])])
        except Exception:
            continue
    return normalized if len(normalized) >= 4 else []


def run_rapidocr(img: Image.Image) -> tuple[list[dict[str, Any]], str]:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:
        return [], "local_ocr_unavailable"
    try:
        ocr = run_rapidocr._ocr  # type: ignore[attr-defined]
    except AttributeError:
        ocr = RapidOCR()
        run_rapidocr._ocr = ocr  # type: ignore[attr-defined]
    try:
        result, _ = ocr(np.asarray(img.convert("RGB")))
    except Exception as exc:
        return [], f"local_ocr_failed:{exc}"
    items: list[dict[str, Any]] = []
    for item in result or []:
        if not isinstance(item, list) or len(item) < 3:
            continue
        bbox = normalize_box(item[0])
        if not bbox:
            continue
        items.append(
            {
                "bbox": bbox,
                "text": str(item[1]).strip(),
                "confidence": float(item[2]),
            }
        )
    return items, ""


def average_hash(img: Image.Image, hash_size: int = 8) -> str:
    reduced = img.convert("L").resize((hash_size, hash_size))
    arr = np.asarray(reduced, dtype=np.float32)
    threshold = float(arr.mean())
    bits = "".join("1" if value >= threshold else "0" for value in arr.flatten())
    return f"{int(bits, 2):0{hash_size * hash_size // 4}x}"


def normalize_text(text: str) -> str:
    value = re.sub(r"\s+", "", text)
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("：", ":")
    return value.lower()


def extract_price(texts: list[str]) -> str:
    for text in texts:
        match = PRICE_RE.search(text.replace(" ", ""))
        if match:
            return match.group(1)
    for text in texts:
        compact = text.replace(" ", "")
        if compact.startswith("¥") or compact.startswith("￥"):
            value = compact.lstrip("¥￥")
            value = value.split("/")[0]
            value = value.split("件")[0]
            return value
    return ""


def enhance_for_ocr(img: Image.Image, scale: int = 2) -> Image.Image:
    gray = img.convert("L")
    arr = np.asarray(gray, dtype=np.uint8)
    arr = np.where(arr > 210, 255, np.where(arr < 140, 0, arr)).astype(np.uint8)
    enhanced = Image.fromarray(arr)
    return enhanced.resize((enhanced.width * scale, enhanced.height * scale))


def extract_spec(texts: list[str]) -> str:
    for text in texts:
        match = SPEC_RE.search(text.replace(" ", ""))
        if match:
            return match.group(0)
    return ""


def extract_price_from_ocr_items(ocr_items: list[dict[str, Any]]) -> str:
    texts = [str(item["text"]).strip() for item in sort_ocr_lines(ocr_items) if str(item["text"]).strip()]
    return extract_price(texts)


def extract_price_from_crop(card_crop: Image.Image) -> tuple[str, str]:
    width, height = card_crop.size
    price_crop = card_crop.crop((0, int(height * 0.34), int(width * 0.68), int(height * 0.82)))
    focused_price_crop = detect_red_price_crop(price_crop)
    ocr_items, warning = run_rapidocr(focused_price_crop)
    price_text = extract_price_from_ocr_items(ocr_items)
    if price_text:
        return price_text, "price_red_focus_ocr"
    enhanced_focus = enhance_for_ocr(focused_price_crop, scale=3)
    ocr_items, warning_focus = run_rapidocr(enhanced_focus)
    price_text = extract_price_from_ocr_items(ocr_items)
    if price_text:
        return price_text, "price_red_focus_enhanced_ocr"
    ocr_items, warning = run_rapidocr(price_crop)
    price_text = extract_price_from_ocr_items(ocr_items)
    if price_text:
        return price_text, "price_crop_ocr"
    enhanced = enhance_for_ocr(price_crop, scale=3)
    ocr_items, warning2 = run_rapidocr(enhanced)
    price_text = extract_price_from_ocr_items(ocr_items)
    if price_text:
        return price_text, "price_crop_enhanced_ocr"
    if warning_focus or warning or warning2:
        return "", warning_focus or warning or warning2
    return "", ""


def detect_red_price_crop(price_crop: Image.Image) -> Image.Image:
    arr = np.asarray(price_crop.convert("RGB"))
    red_mask = (arr[:, :, 0] > 180) & (arr[:, :, 1] < 120) & (arr[:, :, 2] < 120)
    ys, xs = np.where(red_mask)
    if len(xs) < 12:
        return price_crop
    left = max(0, int(xs.min()) - 24)
    right = min(price_crop.width, int(xs.max()) + 140)
    top = max(0, int(ys.min()) - 24)
    bottom = min(price_crop.height, int(ys.max()) + 60)
    if right - left < 80 or bottom - top < 40:
        return price_crop
    return price_crop.crop((left, top, right, bottom))


def sort_ocr_lines(ocr_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        ocr_items,
        key=lambda item: (
            int(min(point[1] for point in item["bbox"])),
            int(min(point[0] for point in item["bbox"])),
        ),
    )


def detect_recommendation_section_y(ocr_items: list[dict[str, Any]]) -> int | None:
    recommendation_tokens = ["为你推荐", "猜你喜欢", "相关推荐"]
    search_recommendation_tokens = ["的人也在搜"]
    candidates: list[int] = []
    for item in ocr_items:
        text = str(item["text"]).replace(" ", "")
        if not text:
            continue
        matched = any(token in text for token in recommendation_tokens)
        matched = matched or any(token in text for token in search_recommendation_tokens)
        if not matched:
            continue
        candidates.append(int(min(point[1] for point in item["bbox"])))
    return min(candidates) if candidates else None


def detect_add_button_anchors(img: Image.Image) -> list[tuple[int, int]]:
    arr = np.asarray(img.convert("RGB"))
    mask = (arr[:, :, 1] > 170) & (arr[:, :, 0] < 120) & (arr[:, :, 2] < 140)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return []
    points = sorted(zip(xs.tolist(), ys.tolist()), key=lambda item: item[1])
    clusters: list[list[tuple[int, int]]] = []
    for x, y in points:
        if x < int(arr.shape[1] * 0.75):
            continue
        if not clusters:
            clusters.append([(x, y)])
            continue
        prev_x, prev_y = clusters[-1][-1]
        if abs(y - prev_y) <= 24 and abs(x - prev_x) <= 40:
            clusters[-1].append((x, y))
        else:
            clusters.append([(x, y)])
    anchors: list[tuple[int, int]] = []
    for cluster in clusters:
        if len(cluster) < 40:
            continue
        xs_local = [item[0] for item in cluster]
        ys_local = [item[1] for item in cluster]
        anchors.append((int(sum(xs_local) / len(xs_local)), int(sum(ys_local) / len(ys_local))))
    deduped: list[tuple[int, int]] = []
    for x, y in anchors:
        if deduped and abs(y - deduped[-1][1]) < 80:
            prev_x, prev_y = deduped[-1]
            deduped[-1] = (int((prev_x + x) / 2), int((prev_y + y) / 2))
        else:
            deduped.append((x, y))
    return deduped


def count_green_button_clusters(img: Image.Image) -> int:
    arr = np.asarray(img.convert("RGB"))
    mask = (arr[:, :, 1] > 170) & (arr[:, :, 0] < 120) & (arr[:, :, 2] < 140)
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return 0
    points = sorted(zip(xs.tolist(), ys.tolist()), key=lambda item: (item[1], item[0]))
    clusters: list[list[tuple[int, int]]] = []
    for x, y in points:
        for cluster in reversed(clusters[-6:]):
            prev_x, prev_y = cluster[-1]
            if abs(y - prev_y) <= 32 and abs(x - prev_x) <= 72:
                cluster.append((x, y))
                break
        else:
            clusters.append([(x, y)])
    return sum(1 for cluster in clusters if len(cluster) >= 40)


def find_card_container_bbox(
    img: Image.Image,
    anchor: tuple[int, int],
    prev_anchor_y: int | None = None,
    next_anchor_y: int | None = None,
) -> list[int]:
    width, height = img.size
    center_x, center_y = anchor
    left = max(0, int(width * 0.06))
    right = min(width, center_x + int(width * 0.05))
    arr = np.asarray(img.convert("RGB"))
    x1 = max(0, int(width * 0.08))
    x2 = min(width, int(width * 0.94))
    region = arr[:, x1:x2]
    white_score = (
        (region[:, :, 0] > 252)
        & (region[:, :, 1] > 252)
        & (region[:, :, 2] > 252)
    ).mean(axis=1)

    def smooth_score(y: int) -> float:
        start = max(0, y - 4)
        end = min(height, y + 5)
        return float(white_score[start:end].mean())

    top_search = max(0, center_y - 520)
    bottom_search = min(height - 1, center_y + 360)
    top = top_search
    low_run = 0
    for y in range(center_y, top_search, -1):
        if smooth_score(y) < 0.34:
            low_run += 1
            if low_run >= 8:
                top = min(height - 1, y + low_run + 2)
                break
        else:
            low_run = 0
    bottom = bottom_search
    low_run = 0
    for y in range(center_y, bottom_search):
        if smooth_score(y) < 0.34:
            low_run += 1
            if low_run >= 8:
                bottom = max(1, y - low_run - 2)
                break
        else:
            low_run = 0
    if next_anchor_y is not None:
        next_top_estimate = max(0, next_anchor_y - 470)
        bottom = min(bottom, max(top + 220, next_top_estimate - 12))
    if prev_anchor_y is None:
        top = min(top, max(0, center_y - 430))
    else:
        # Dense title text can look like a separator. If that happens, recall
        # the card top from the previous card's add-button rhythm instead.
        # Skip abnormal gaps, which usually mean a recommendation/search block
        # sits between the two product cards.
        anchor_gap = max(1, center_y - prev_anchor_y)
        if anchor_gap <= 520:
            rhythmic_top = prev_anchor_y + int(anchor_gap * 0.18)
            if top - rhythmic_top > 48:
                top = max(0, rhythmic_top)
    bottom = max(bottom, min(height, center_y + 78))
    if bottom - top < 260:
        extra = 260 - (bottom - top)
        top = max(0, top - extra // 2)
        bottom = min(height, bottom + extra - extra // 2)
    return [left, max(0, top), right, min(height, bottom)]


def crop_card_around_anchor(
    img: Image.Image,
    anchor: tuple[int, int],
    prev_anchor_y: int | None = None,
    next_anchor_y: int | None = None,
) -> tuple[Image.Image, list[int]]:
    left, top, right, bottom = find_card_container_bbox(
        img,
        anchor,
        prev_anchor_y=prev_anchor_y,
        next_anchor_y=next_anchor_y,
    )
    return img.crop((left, top, right, bottom)), [left, top, right, bottom]


def build_title_text(lines: list[str], brand_hint: str) -> str:
    for line in lines:
        compact = line.replace(" ", "")
        if brand_hint and brand_hint in compact:
            return compact
    for line in lines:
        compact = line.replace(" ", "")
        if PRICE_RE.search(compact):
            continue
        if compact.startswith("¥"):
            continue
        if len(compact) >= 6:
            return compact
    return ""


def determine_candidate_validity(
    *,
    texts: list[str],
    title_text: str,
    price_text: str,
    spec_text: str,
    brand_hint: str,
) -> tuple[bool, str, bool]:
    compact_title = title_text.replace(" ", "")
    brand_hint_matched = bool(brand_hint and brand_hint in compact_title)
    compact_texts = [text.replace(" ", "") for text in texts if text]
    exclusion_tokens = ["更多", "吃点解解馋"]
    if any(token in compact_title for token in exclusion_tokens):
        return False, "non_sku_promo_text", brand_hint_matched
    recommendation_tokens = ["为你推荐", "猜你喜欢", "相关推荐", "的人也在搜"]
    if any(any(token in text for token in recommendation_tokens) for text in compact_texts):
        return False, "recommendation_section", brand_hint_matched
    ui_tokens = ["搜索", "综合", "价格", "销量", "折扣"]
    ui_hits = sum(1 for token in ui_tokens if any(token in text for text in compact_texts))
    if ui_hits >= 2:
        return False, "ui_control_strip", brand_hint_matched
    if not price_text:
        return False, "price_missing", brand_hint_matched
    if len(compact_title) <= 2:
        return False, "title_too_short", brand_hint_matched
    return True, "", brand_hint_matched


def infer_product_type_hint(
    *,
    texts: list[str],
    title_text: str,
    is_recommendation_zone_card: bool,
    is_valid_sku: bool,
    exclusion_reason: str,
) -> str:
    if is_recommendation_zone_card or exclusion_reason == "recommendation_section":
        return "recommendation_card"
    if not is_valid_sku:
        return "non_sku_module"
    compact = "".join(text.replace(" ", "") for text in [title_text, *texts] if text)
    combo_tokens = ["套餐", "组合", "混合", "组合装", "6种商品", "多种商品", "礼包", "组合包", "决战套", "世界杯"]
    if any(token in compact for token in combo_tokens):
        return "bundle_or_combo"
    return "normal_sku" if title_text else "unknown"


def quality_score_for_card(
    *,
    has_title: bool,
    has_price: bool,
    is_truncated_top: bool,
    is_truncated_bottom: bool,
    text_confidence: float,
) -> float:
    score = 0.0
    if has_title:
        score += 3.0
    if has_price:
        score += 2.0
    score += min(text_confidence, 0.99)
    if is_truncated_top:
        score -= 2.0
    if is_truncated_bottom:
        score -= 2.0
    return round(score, 3)


def score_card_completeness(
    *,
    bbox: list[int],
    page_height: int,
    has_title: bool,
    has_price: bool,
    has_add_button: bool,
    is_valid_sku: bool,
    exclusion_reason: str,
    weak_identity: bool,
    trimmed_bottom_ui: bool,
    median_height: float,
) -> tuple[int, list[str], bool]:
    reasons: list[str] = []
    score = 0
    height = max(0, bbox[3] - bbox[1])
    if has_title:
        score += 25
    else:
        reasons.append("title_missing")
    if has_price:
        score += 20
    else:
        reasons.append("price_missing")
    if has_add_button:
        score += 15
    else:
        reasons.append("add_button_missing")
    if bbox[1] > 12:
        score += 12
    else:
        reasons.append("top_truncated")
    if bbox[3] < page_height - 12:
        score += 12
    else:
        reasons.append("bottom_truncated")
    if median_height > 0 and height < median_height * 0.72:
        reasons.append("height_too_short")
    else:
        score += 8
    if weak_identity:
        reasons.append("brand_text_missing")
    score += 4
    if not trimmed_bottom_ui:
        score += 4
    else:
        reasons.append("bottom_ui_trimmed")
    if is_valid_sku:
        score += 4
    else:
        reasons.append(exclusion_reason or "invalid_sku")
    score = max(0, min(100, score))
    is_complete = score >= 78 and not any(
        reason in reasons
        for reason in [
            "title_missing",
            "price_missing",
            "top_truncated",
            "bottom_truncated",
            "height_too_short",
            "recommendation_section",
            "ui_control_strip",
        ]
    )
    return score, reasons, is_complete


def save_preview(card_crop: Image.Image, preview_path: Path) -> None:
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    card_crop.save(preview_path)


def maybe_trim_bottom_ui(card_crop: Image.Image, ocr_items: list[dict[str, Any]]) -> tuple[Image.Image, bool]:
    footer_tokens = ["搜索", "综合", "价格", "销量", "折扣", "为你推荐", "猜你喜欢", "相关推荐"]
    height = card_crop.height
    trim_y: int | None = None
    for item in ocr_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not any(token in text for token in footer_tokens):
            continue
        box_top = int(min(point[1] for point in item["bbox"]))
        if box_top < int(height * 0.72):
            continue
        candidate_y = max(0, box_top - 18)
        trim_y = candidate_y if trim_y is None else min(trim_y, candidate_y)
    if trim_y is None or trim_y < int(height * 0.68):
        return card_crop, False
    trimmed = card_crop.crop((0, 0, card_crop.width, trim_y))
    return trimmed, True


def maybe_trim_top_ui(card_crop: Image.Image, ocr_items: list[dict[str, Any]]) -> tuple[Image.Image, int]:
    top_tokens = ["搜索", "综合", "价格", "销量", "折扣"]
    height = card_crop.height
    trim_y = 0
    for item in ocr_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not any(token in text for token in top_tokens):
            continue
        box_bottom = int(max(point[1] for point in item["bbox"]))
        if box_bottom > int(height * 0.34):
            continue
        trim_y = max(trim_y, box_bottom + 18)
    if trim_y <= 0 or trim_y > int(height * 0.38):
        return card_crop, 0
    return card_crop.crop((0, trim_y, card_crop.width, card_crop.height)), trim_y


def analyze_page(
    page_path: Path,
    page_index: int,
    brand_hint: str,
    preview_root: Path,
) -> tuple[list[CardCandidate], list[str]]:
    warnings: list[str] = []
    with Image.open(page_path) as img:
        rgb = img.convert("RGB")
        page_ocr_items, page_ocr_warning = run_rapidocr(rgb)
        if page_ocr_warning:
            warnings.append(f"page_{page_ocr_warning}")
        recommendation_section_y = detect_recommendation_section_y(page_ocr_items)
        anchors = detect_add_button_anchors(rgb)
        if not anchors:
            return [], ["no_add_button_anchor_detected"]
        anchor_bboxes: list[list[int]] = []
        for anchor_index, anchor in enumerate(anchors, start=1):
            prev_anchor_y = anchors[anchor_index - 2][1] if anchor_index > 1 else None
            next_anchor_y = anchors[anchor_index][1] if anchor_index < len(anchors) else None
            anchor_bboxes.append(
                find_card_container_bbox(
                    rgb,
                    anchor,
                    prev_anchor_y=prev_anchor_y,
                    next_anchor_y=next_anchor_y,
                )
            )
        heights = [bbox[3] - bbox[1] for bbox in anchor_bboxes if bbox[3] > bbox[1]]
        median_height = float(np.median(heights)) if heights else 0.0
        candidates: list[CardCandidate] = []
        for card_index, anchor in enumerate(anchors, start=1):
            bbox = anchor_bboxes[card_index - 1]
            crop = rgb.crop(tuple(bbox))
            ocr_items, ocr_warning = run_rapidocr(crop)
            if ocr_warning:
                warnings.append(f"card_{card_index}_{ocr_warning}")
            crop, trimmed_top_y = maybe_trim_top_ui(crop, ocr_items)
            if trimmed_top_y:
                bbox[1] += trimmed_top_y
                ocr_items, ocr_warning = run_rapidocr(crop)
                if ocr_warning:
                    warnings.append(f"card_{card_index}_{ocr_warning}")
            crop, trimmed_bottom_ui = maybe_trim_bottom_ui(crop, ocr_items)
            if trimmed_bottom_ui:
                bbox[3] = bbox[1] + crop.height
                ocr_items, ocr_warning = run_rapidocr(crop)
                if ocr_warning:
                    warnings.append(f"card_{card_index}_{ocr_warning}")
            preview_path = preview_root / f"page_{page_index:02d}" / f"card_{card_index:02d}.png"
            save_preview(crop, preview_path)
            lines = sort_ocr_lines(ocr_items)
            texts = [str(item["text"]).strip() for item in lines if str(item["text"]).strip()]
            confidences = [float(item["confidence"]) for item in lines if str(item["text"]).strip()]
            title_text = build_title_text(texts, brand_hint)
            price_text = extract_price(texts)
            price_source = "full_card_ocr" if price_text else ""
            if not price_text:
                fallback_price, fallback_source = extract_price_from_crop(crop)
                if fallback_price:
                    price_text = fallback_price
                    price_source = fallback_source
            spec_text = extract_spec(texts)
            has_title = bool(title_text)
            has_price = bool(price_text)
            is_valid_sku, exclusion_reason, brand_hint_matched = determine_candidate_validity(
                texts=texts,
                title_text=title_text,
                price_text=price_text,
                spec_text=spec_text,
                brand_hint=brand_hint,
            )
            is_recommendation_zone_card = recommendation_section_y is not None and bbox[1] > recommendation_section_y
            if is_recommendation_zone_card:
                is_valid_sku = False
                exclusion_reason = "recommendation_section"
            is_main_result_card = not is_recommendation_zone_card
            product_type_hint = infer_product_type_hint(
                texts=texts,
                title_text=title_text,
                is_recommendation_zone_card=is_recommendation_zone_card,
                is_valid_sku=is_valid_sku,
                exclusion_reason=exclusion_reason,
            )
            brand_text_missing = not brand_hint_matched
            brand_text_missing_but_main_result_kept = bool(
                is_valid_sku and is_main_result_card and brand_text_missing
            )
            is_truncated_top = bbox[1] <= 8
            is_truncated_bottom = bbox[3] >= img.height - 8
            if is_valid_sku and is_truncated_bottom and count_green_button_clusters(crop) >= 2:
                is_valid_sku = False
                exclusion_reason = "bottom_edge_multi_card_fragment"
            card_warnings: list[str] = []
            if is_truncated_top:
                card_warnings.append("touch_top_edge")
            if is_truncated_bottom:
                card_warnings.append("touch_bottom_edge")
            if not has_title:
                card_warnings.append("title_missing")
            if not has_price:
                card_warnings.append("price_missing")
            if not is_valid_sku and exclusion_reason:
                card_warnings.append(f"excluded:{exclusion_reason}")
            if brand_text_missing_but_main_result_kept:
                card_warnings.append("brand_text_missing_but_main_result_kept")
            if trimmed_bottom_ui:
                card_warnings.append("bottom_ui_trimmed")
            if trimmed_top_y:
                card_warnings.append("top_ui_trimmed")
            text_confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
            completeness_score, incomplete_reason, is_complete_card = score_card_completeness(
                bbox=bbox,
                page_height=img.height,
                has_title=has_title,
                has_price=has_price,
                has_add_button=True,
                is_valid_sku=is_valid_sku,
                exclusion_reason=exclusion_reason,
                weak_identity=brand_text_missing,
                trimmed_bottom_ui=trimmed_bottom_ui,
                median_height=median_height,
            )
            is_complete = is_complete_card
            candidate = CardCandidate(
                card_id=f"p{page_index:02d}_c{card_index:02d}",
                page_index=page_index,
                card_index=card_index,
                source_path=str(page_path),
                bbox=bbox,
                anchor_center=[anchor[0], anchor[1]],
                title_text=title_text,
                price_text=price_text,
                spec_text=spec_text,
                normalized_title=normalize_text(title_text),
                normalized_spec=normalize_text(spec_text),
                image_hash=average_hash(crop),
                text_confidence=text_confidence,
                is_truncated_top=is_truncated_top,
                is_truncated_bottom=is_truncated_bottom,
                has_title=has_title,
                has_price=has_price,
                is_complete=is_complete,
                is_complete_card=is_complete_card,
                completeness_score=completeness_score,
                incomplete_reason=incomplete_reason,
                has_full_duplicate=False,
                action="pending",
                action_reason="",
                quality_score=quality_score_for_card(
                    has_title=has_title,
                    has_price=has_price,
                    is_truncated_top=is_truncated_top,
                    is_truncated_bottom=is_truncated_bottom,
                    text_confidence=text_confidence,
                ),
                price_source=price_source,
                preview_path=str(preview_path),
                brand_hint_matched=brand_hint_matched,
                is_main_result_card=is_main_result_card,
                brand_text_missing=brand_text_missing,
                brand_text_missing_but_main_result_kept=brand_text_missing_but_main_result_kept,
                product_type_hint=product_type_hint,
                is_valid_sku=is_valid_sku,
                exclusion_reason=exclusion_reason,
                is_recommendation_zone_card=exclusion_reason == "recommendation_section",
                warnings=card_warnings,
            )
            candidates.append(candidate)
    return candidates, warnings


def group_key_for_card(card: CardCandidate) -> str:
    if card.normalized_title and card.price_text and card.normalized_spec:
        return f"{card.normalized_title}|{card.price_text}|{card.normalized_spec}"
    if card.normalized_title and card.price_text:
        return f"{card.normalized_title}|{card.price_text}"
    if card.normalized_title:
        return f"{card.normalized_title}|{card.image_hash}"
    return f"unknown|{card.image_hash}"


def duplicate_key_for_card(card: CardCandidate) -> str:
    if card.normalized_title and card.normalized_spec:
        return f"{card.normalized_title}|{card.normalized_spec}"
    if card.normalized_title and card.price_text:
        return f"{card.normalized_title}|{card.price_text}"
    if card.normalized_title:
        return card.normalized_title
    return f"unknown|{card.image_hash}"


def hamming_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        return max(len(left), len(right))
    return sum(1 for a, b in zip(left, right) if a != b)


def title_contains_relation(left: CardCandidate, right: CardCandidate) -> bool:
    a = left.normalized_title
    b = right.normalized_title
    return bool(a and b and len(a) >= 4 and len(b) >= 4 and (a in b or b in a))


def title_completeness_rank(card: CardCandidate) -> tuple[int, int, int, float]:
    return (
        len(card.normalized_spec),
        len(card.normalized_title),
        0 if "top_ui_trimmed" in card.warnings else 1,
        card.quality_score,
    )


def is_overlap_fragment_duplicate(left: CardCandidate, right: CardCandidate) -> bool:
    if not left.is_complete_card or not right.is_complete_card:
        return False
    if left.action != "keep" or right.action != "keep":
        return False
    if left.page_index == right.page_index:
        return False
    same_title = bool(left.normalized_title and left.normalized_title == right.normalized_title)
    contains_title = title_contains_relation(left, right)
    same_price = bool(left.price_text and left.price_text == right.price_text)
    similar_image = hamming_distance(left.image_hash, right.image_hash) <= 14
    if same_title and similar_image:
        return True
    if contains_title and same_price:
        return True
    return False


def exclude_overlap_fragment_duplicates(cards: list[CardCandidate]) -> list[dict[str, Any]]:
    removals: list[dict[str, Any]] = []
    changed = True
    while changed:
        changed = False
        kept_cards = [card for card in cards if card.action == "keep"]
        for index, left in enumerate(kept_cards):
            for right in kept_cards[index + 1 :]:
                if not is_overlap_fragment_duplicate(left, right):
                    continue
                keep, drop = sorted(
                    [left, right],
                    key=lambda card: title_completeness_rank(card),
                    reverse=True,
                )
                drop.action = "exclude"
                drop.action_reason = "overlap_fragment_duplicate"
                drop.has_full_duplicate = True
                drop.warnings.append("excluded:overlap_fragment_duplicate")
                removals.append(
                    {
                        "kept_card_id": keep.card_id,
                        "dropped_card_id": drop.card_id,
                        "kept_title": keep.title_text,
                        "dropped_title": drop.title_text,
                        "reason": "overlap fragment duplicate of a more complete kept card",
                    }
                )
                changed = True
                break
            if changed:
                break
    return removals


def build_decision(cards: list[CardCandidate]) -> dict[str, Any]:
    for card in cards:
        if not card.is_valid_sku:
            card.action = "exclude"
            card.action_reason = card.exclusion_reason or "invalid_sku"

    excluded_cards = [card for card in cards if not card.is_valid_sku]
    valid_cards = [card for card in cards if card.is_valid_sku]
    grouped: dict[str, list[CardCandidate]] = defaultdict(list)
    for card in valid_cards:
        grouped[duplicate_key_for_card(card)].append(card)

    sku_groups: list[dict[str, Any]] = []
    duplicate_group_count = 0
    dropped_duplicate_cards = 0
    incomplete_cards_excluded = 0
    incomplete_cards_warning = 0
    possible_missing_sku_warnings: list[dict[str, Any]] = []
    needs_manual_review = False
    delivery_grade = "pass"
    duplicate_removals: list[dict[str, Any]] = []

    for group_id, group_cards in grouped.items():
        group_cards = sorted(
            group_cards,
            key=lambda item: (-item.completeness_score, -item.quality_score, item.page_index, item.card_index),
        )
        complete_cards = [card for card in group_cards if card.is_complete_card]
        incomplete_cards = [card for card in group_cards if not card.is_complete_card]
        selected_card = complete_cards[0] if complete_cards else None
        dropped_cards: list[str] = []
        incomplete_excluded_ids: list[str] = []
        warning_card_ids: list[str] = []
        decision_reason: list[str] = []
        if len(complete_cards) > 1:
            duplicate_group_count += 1
        for card in incomplete_cards:
            card.has_full_duplicate = selected_card is not None
            if card.has_full_duplicate:
                card.action = "exclude"
                card.action_reason = "incomplete_duplicate"
                incomplete_cards_excluded += 1
                incomplete_excluded_ids.append(card.card_id)
            else:
                card.action = "warning"
                card.action_reason = "possible_missing_sku"
                incomplete_cards_warning += 1
                warning_card_ids.append(card.card_id)
                needs_manual_review = True
                possible_missing_sku_warnings.append(
                    {
                        "card_id": card.card_id,
                        "title": card.title_text,
                        "page_index": card.page_index,
                        "y": card.bbox[1],
                        "reason": ";".join(card.incomplete_reason),
                    }
                )
        if selected_card is not None:
            selected_card.action = "keep"
            if selected_card.product_type_hint == "bundle_or_combo" and selected_card.brand_text_missing_but_main_result_kept:
                selected_card.action_reason = "main_result_bundle_kept;brand_text_missing_but_main_result_kept"
            elif selected_card.brand_text_missing_but_main_result_kept:
                selected_card.action_reason = "brand_text_missing_but_main_result_kept"
            else:
                selected_card.action_reason = "selected_best_complete_card"
            for card in complete_cards:
                if card.card_id == selected_card.card_id:
                    continue
                card.action = "exclude"
                card.action_reason = "duplicate_sku"
                dropped_cards.append(card.card_id)
                dropped_duplicate_cards += 1
                duplicate_removals.append(
                    {
                        "sku_group_id": group_id,
                        "kept_card_id": selected_card.card_id,
                        "dropped_card_id": card.card_id,
                        "title": card.title_text,
                        "reason": "selected higher completeness/quality",
                    }
                )
            if dropped_cards:
                decision_reason.append("duplicate_sku_removed")
            if incomplete_excluded_ids:
                decision_reason.append("incomplete_duplicates_excluded")
        else:
            needs_manual_review = True
            decision_reason.append("no_complete_card_possible_missing_sku")
            delivery_grade = "fail"
        sku_groups.append(
            {
                "sku_group_id": group_id,
                "card_ids": [card.card_id for card in group_cards],
                "selected_card_id": selected_card.card_id if selected_card else "",
                "dropped_card_ids": dropped_cards,
                "incomplete_excluded_card_ids": incomplete_excluded_ids,
                "warning_card_ids": warning_card_ids,
                "decision_reason": ";".join(decision_reason),
                "needs_manual_review": selected_card is None,
                "candidate_count": len(group_cards),
                "complete_count": len(complete_cards),
                "incomplete_count": len(incomplete_cards),
                "title_examples": [card.title_text for card in group_cards if card.title_text][:2],
            }
        )

    overlap_fragment_removals = exclude_overlap_fragment_duplicates(cards)
    if overlap_fragment_removals:
        dropped_duplicate_cards += len(overlap_fragment_removals)
        duplicate_removals.extend(
            {
                "sku_group_id": "overlap_fragment_duplicate",
                "kept_card_id": item["kept_card_id"],
                "dropped_card_id": item["dropped_card_id"],
                "title": item["dropped_title"],
                "reason": item["reason"],
            }
            for item in overlap_fragment_removals
        )

    if delivery_grade == "pass" and (duplicate_group_count > 0 or incomplete_cards_excluded > 0 or dropped_duplicate_cards > 0):
        delivery_grade = "fixable"
    if needs_manual_review:
        delivery_grade = "fail"
    kept_count = sum(1 for card in cards if card.action == "keep")
    detail_noise_count = sum(
        1
        for card in cards
        if card.action == "exclude"
        and card.action_reason == "price_missing"
        and (
            card.is_truncated_bottom
            or "图片仅" in card.title_text
            or "质量体系" in card.title_text
            or "源头产地直采" in card.title_text
            or "支持自助售后" in card.title_text
        )
    )
    detail_page_warning = kept_count <= 2 and len(cards) >= 12 and detail_noise_count >= 6
    if detail_page_warning:
        needs_manual_review = True
        delivery_grade = "fail"

    return {
        "delivery_grade": delivery_grade,
        "needs_manual_review": needs_manual_review,
        "total_candidate_cards": len(cards),
        "complete_cards_kept": kept_count,
        "incomplete_cards_excluded": incomplete_cards_excluded,
        "incomplete_cards_warning": incomplete_cards_warning,
        "duplicate_skus_removed": dropped_duplicate_cards,
        "recommendation_cards_excluded": sum(1 for card in excluded_cards if card.exclusion_reason == "recommendation_section"),
        "possible_missing_sku_warnings": possible_missing_sku_warnings,
        "rebuilt_cards_count": kept_count,
        "detail_page_warning": detail_page_warning,
        "detail_page_noise_cards": detail_noise_count,
        "excluded_cards": len(excluded_cards),
        "excluded_card_ids": [card.card_id for card in excluded_cards],
        "duplicate_sku_groups": duplicate_group_count,
        "dropped_duplicate_cards": dropped_duplicate_cards,
        "dropped_truncated_cards": incomplete_cards_excluded,
        "overlap_fragment_duplicates_removed": len(overlap_fragment_removals),
        "overlap_fragment_duplicate_removals": overlap_fragment_removals,
        "duplicate_removals": duplicate_removals,
        "sku_groups": sku_groups,
    }


def infer_pages_dir(date_str: str, brand: str, city: str, override: str) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return (DEBUG_H5_PAGES_DIR / date_str / brand / city).resolve()


def default_output_paths(
    date_str: str,
    brand: str,
    city: str,
    output_json: str,
    output_csv: str,
    output_md: str,
) -> tuple[Path, Path, Path, Path]:
    root = REPORTS_DIR / date_str / "h5_cleaning" / brand
    root.mkdir(parents=True, exist_ok=True)
    json_path = Path(output_json).expanduser().resolve() if output_json else (root / f"{city}.json")
    csv_path = Path(output_csv).expanduser().resolve() if output_csv else (root / f"{city}.csv")
    md_path = Path(output_md).expanduser().resolve() if output_md else (root / f"{city}.md")
    preview_root = root / city
    json_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    preview_root.mkdir(parents=True, exist_ok=True)
    return json_path, csv_path, md_path, preview_root


def city_short_name(city: str) -> str:
    return city[:-1] if city.endswith("市") else city


def source_h5_path_for(date_str: str, brand: str, city: str) -> Path:
    filename = f"{brand}（{city_short_name(city)} {date_str}）H5长图.png"
    return DELIVERABLES_H5_DIR / brand / date_str / city / filename


def cleaned_h5_path_for(date_str: str, brand: str, city: str) -> Path:
    filename = f"{brand}（{city_short_name(city)} {date_str}）H5清洗图.png"
    return DELIVERABLES_H5_CLEANED_DIR / brand / date_str / city / filename


def analyze_sample(
    *,
    date_str: str,
    brand: str,
    city: str,
    pages_dir_override: str = "",
    output_json: str = "",
    output_csv: str = "",
    output_md: str = "",
) -> dict[str, Any]:
    pages_dir = infer_pages_dir(date_str, brand, city, pages_dir_override)
    if not pages_dir.exists():
        raise FileNotFoundError(f"页图目录不存在: {pages_dir}")
    page_paths = sorted(pages_dir.glob("*.png"))
    if not page_paths:
        raise FileNotFoundError(f"页图目录下没有 PNG: {pages_dir}")

    cards: list[CardCandidate] = []
    page_warnings: dict[str, list[str]] = {}
    json_path, csv_path, md_path, preview_root = default_output_paths(
        date_str,
        brand,
        city,
        output_json,
        output_csv,
        output_md,
    )
    for index, page_path in enumerate(page_paths, start=1):
        page_cards, warnings = analyze_page(page_path, index, brand, preview_root)
        cards.extend(page_cards)
        if warnings:
            page_warnings[page_path.name] = warnings

    decision = build_decision(cards)
    payload: dict[str, Any] = {
        "date": date_str,
        "brand": brand,
        "city": city,
        "pages_dir": str(pages_dir),
        "source_h5_path": str(source_h5_path_for(date_str, brand, city)),
        "page_count": len(page_paths),
        "card_count": len(cards),
        "preview_root": str(preview_root),
        "page_warnings": page_warnings,
        "cards": [asdict(card) for card in cards],
        "summary": decision,
    }
    rebuilt_preview_path = build_reconstructed_preview(
        cards=cards,
        decision=decision,
        output_path=preview_root.parent / f"{city}.rebuilt.png",
    )
    if rebuilt_preview_path:
        payload["rebuilt_preview_path"] = rebuilt_preview_path
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(cards, csv_path)
    write_markdown(
        md_path=md_path,
        date_str=date_str,
        brand=brand,
        city=city,
        pages_dir=pages_dir,
        cards=cards,
        page_warnings=page_warnings,
        decision=decision,
    )
    payload["output_json"] = str(json_path)
    payload["output_csv"] = str(csv_path)
    payload["output_md"] = str(md_path)
    return payload


def batch_summary_row(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload["summary"]
    return {
        "date": payload["date"],
        "brand": payload["brand"],
        "city": payload["city"],
        "delivery_grade": summary["delivery_grade"],
        "source_h5_path": payload.get("source_h5_path", ""),
        "rebuilt_image_path": payload.get("rebuilt_preview_path", ""),
        "complete_cards_kept": summary["complete_cards_kept"],
        "duplicate_skus_removed": summary["duplicate_skus_removed"],
        "incomplete_cards_excluded": summary["incomplete_cards_excluded"],
        "incomplete_cards_warning": summary["incomplete_cards_warning"],
        "recommendation_cards_excluded": summary["recommendation_cards_excluded"],
        "possible_missing_sku_warnings": len(summary.get("possible_missing_sku_warnings", [])),
        "needs_manual_review": str(summary["needs_manual_review"]).lower(),
        "json_path": payload.get("output_json", ""),
    }


def write_batch_summary(date_str: str, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    root = REPORTS_DIR / date_str / "h5_cleaning"
    root.mkdir(parents=True, exist_ok=True)
    csv_path = root / "_batch_summary.csv"
    json_path = root / "_batch_summary.json"
    fields = [
        "date",
        "brand",
        "city",
        "delivery_grade",
        "source_h5_path",
        "rebuilt_image_path",
        "complete_cards_kept",
        "duplicate_skus_removed",
        "incomplete_cards_excluded",
        "incomplete_cards_warning",
        "recommendation_cards_excluded",
        "possible_missing_sku_warnings",
        "needs_manual_review",
        "json_path",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path


def run_batch_analysis(date_str: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    date_root = DEBUG_H5_PAGES_DIR / date_str
    if not date_root.exists():
        raise SystemExit(f"页图日期目录不存在: {date_root}")
    sample_dirs = [
        (brand_dir, city_dir)
        for brand_dir in sorted(path for path in date_root.iterdir() if path.is_dir())
        for city_dir in sorted(path for path in brand_dir.iterdir() if path.is_dir())
    ]
    for sample_index, (brand_dir, city_dir) in enumerate(sample_dirs, start=1):
        print(f"analyzing_sample={sample_index}/{len(sample_dirs)} brand={brand_dir.name} city={city_dir.name}", flush=True)
        try:
            payload = analyze_sample(date_str=date_str, brand=brand_dir.name, city=city_dir.name)
            rows.append(batch_summary_row(payload))
            print(
                f"analyzed_sample={sample_index}/{len(sample_dirs)} brand={brand_dir.name} "
                f"city={city_dir.name} grade={payload['summary']['delivery_grade']}",
                flush=True,
            )
        except Exception as exc:
            failures.append({"brand": brand_dir.name, "city": city_dir.name, "error": str(exc)})
    csv_path, json_path = write_batch_summary(date_str, rows)
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[str(row["delivery_grade"])] += 1
    return {
        "rows": rows,
        "failures": failures,
        "csv_path": str(csv_path),
        "json_path": str(json_path),
        "counts": dict(counts),
    }


def write_csv(cards: list[CardCandidate], csv_path: Path) -> None:
    fields = [
        "card_id",
        "page_index",
        "card_index",
        "title_text",
        "price_text",
        "spec_text",
        "normalized_title",
        "normalized_spec",
        "image_hash",
        "text_confidence",
        "is_truncated_top",
        "is_truncated_bottom",
        "has_title",
        "has_price",
        "is_complete",
        "is_complete_card",
        "completeness_score",
        "incomplete_reason",
        "has_full_duplicate",
        "action",
        "action_reason",
        "brand_hint_matched",
        "is_main_result_card",
        "brand_text_missing",
        "brand_text_missing_but_main_result_kept",
        "product_type_hint",
        "is_valid_sku",
        "exclusion_reason",
        "is_recommendation_zone_card",
        "quality_score",
        "price_source",
        "preview_path",
        "source_path",
        "bbox",
        "anchor_center",
        "warnings",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for card in cards:
            row = asdict(card)
            row["bbox"] = json.dumps(card.bbox, ensure_ascii=False)
            row["anchor_center"] = json.dumps(card.anchor_center, ensure_ascii=False)
            row["incomplete_reason"] = ";".join(card.incomplete_reason)
            row["warnings"] = ";".join(card.warnings)
            writer.writerow(row)


def write_markdown(
    *,
    md_path: Path,
    date_str: str,
    brand: str,
    city: str,
    pages_dir: Path,
    cards: list[CardCandidate],
    page_warnings: dict[str, list[str]],
    decision: dict[str, Any],
) -> None:
    card_map = {card.card_id: card for card in cards}
    lines = [
        f"# H5 清洗分析报告: {brand} {city} {date_str}",
        "",
        f"- 页图目录: `{pages_dir}`",
        f"- 候选卡片数: {decision['total_candidate_cards']}",
        f"- 交付分级: `{decision['delivery_grade']}`",
        f"- 需人工复核: `{str(decision['needs_manual_review']).lower()}`",
        f"- 排除候选数: {decision['excluded_cards']}",
        f"- 完整保留卡片数: {decision['complete_cards_kept']}",
        f"- 不完整剔除卡片数: {decision['incomplete_cards_excluded']}",
        f"- 不完整 warning 卡片数: {decision['incomplete_cards_warning']}",
        f"- 推荐区排除卡片数: {decision['recommendation_cards_excluded']}",
        f"- 重复 SKU 组数: {decision['duplicate_sku_groups']}",
        f"- SKU 去重删除数: {decision['duplicate_skus_removed']}",
        "",
    ]
    if page_warnings:
        lines.extend(["## 页级告警", ""])
        for page_name, warnings in sorted(page_warnings.items()):
            lines.append(f"- `{page_name}`: {'; '.join(warnings)}")
        lines.append("")

    incomplete_excluded = [card for card in cards if card.action_reason == "incomplete_duplicate"]
    lines.extend(["## 被剔除的不完整卡片", ""])
    if incomplete_excluded:
        lines.append("| card_id | page | y | title | reason | has_full_duplicate | preview |")
        lines.append("|---|---:|---:|---|---|---|---|")
        for card in incomplete_excluded:
            lines.append(
                f"| {card.card_id} | {card.page_index} | {card.bbox[1]} | {card.title_text or '-'} | "
                f"{'; '.join(card.incomplete_reason) or card.action_reason} | {str(card.has_full_duplicate).lower()} | "
                f"[preview]({card.preview_path}) |"
            )
    else:
        lines.append("- 无")
    lines.append("")

    warning_incomplete = [card for card in cards if card.action_reason == "possible_missing_sku"]
    lines.extend(["## 被保留为 warning 的不完整卡片", ""])
    if warning_incomplete:
        lines.append("| card_id | page | y | title | warning | 为什么没有删除 | preview |")
        lines.append("|---|---:|---:|---|---|---|---|")
        for card in warning_incomplete:
            lines.append(
                f"| {card.card_id} | {card.page_index} | {card.bbox[1]} | {card.title_text or '-'} | "
                f"{'; '.join(card.incomplete_reason) or 'possible_missing_sku'} | 全局未找到完整重复项 | "
                f"[preview]({card.preview_path}) |"
            )
    else:
        lines.append("- 无")
    lines.append("")

    lines.extend(["## 去重删除的重复 SKU", ""])
    if decision["duplicate_removals"]:
        lines.append("| SKU | title | kept | dropped | 保留原因 |")
        lines.append("|---|---|---|---|---|")
        for item in decision["duplicate_removals"]:
            lines.append(
                f"| {item['sku_group_id']} | {item['title'] or '-'} | {item['kept_card_id']} | "
                f"{item['dropped_card_id']} | {item['reason']} |"
            )
    else:
        lines.append("- 无")
    lines.append("")

    recommendation_excluded = [card for card in cards if card.exclusion_reason == "recommendation_section"]
    lines.extend(["## 推荐区排除项", ""])
    if recommendation_excluded:
        for card in recommendation_excluded:
            lines.append(
                f"- `{card.card_id}`: {card.title_text or '-'} page={card.page_index} "
                f"y={card.bbox[1]} [preview]({card.preview_path})"
            )
    else:
        lines.append("- 无")
    lines.append("")

    other_excluded_ids = [
        card_id
        for card_id in decision["excluded_card_ids"]
        if card_map[card_id].exclusion_reason != "recommendation_section"
    ]
    if other_excluded_ids:
        lines.extend(["## 已排除候选", ""])
        for card_id in other_excluded_ids:
            card = card_map[card_id]
            lines.append(
                f"- `{card.card_id}`: {card.title_text or '-'} "
                f"({card.exclusion_reason or 'excluded'}) [preview]({card.preview_path})"
            )
        lines.append("")

    lines.extend(["## SKU 分组", ""])
    for group in decision["sku_groups"]:
        lines.append(f"### {group['sku_group_id']}")
        lines.append("")
        lines.append(f"- 候选数: {group['candidate_count']}")
        lines.append(f"- 完整数: {group['complete_count']}")
        lines.append(f"- 不完整数: {group['incomplete_count']}")
        lines.append(f"- 选中卡片: `{group['selected_card_id'] or '-'}`")
        lines.append(f"- 丢弃卡片: `{', '.join(group['dropped_card_ids']) if group['dropped_card_ids'] else '-'}`")
        lines.append(
            f"- 剔除不完整卡片: `{', '.join(group['incomplete_excluded_card_ids']) if group['incomplete_excluded_card_ids'] else '-'}`"
        )
        lines.append(f"- warning 卡片: `{', '.join(group['warning_card_ids']) if group['warning_card_ids'] else '-'}`")
        lines.append(f"- 决策原因: `{group['decision_reason'] or '-'}`")
        lines.append(f"- 人工复核: `{str(group['needs_manual_review']).lower()}`")
        lines.append("")
        lines.append("| card_id | page | y | title | price | score | action | reason | preview |")
        lines.append("|---|---:|---:|---|---|---:|---|---|---|")
        for card_id in group["card_ids"]:
            card = card_map[card_id]
            preview_link = f"[preview]({card.preview_path})"
            lines.append(
                f"| {card.card_id} | {card.page_index} | {card.bbox[1]} | {card.title_text or '-'} | "
                f"{card.price_text or '-'} | {card.completeness_score} | {card.action or '-'} | "
                f"{card.action_reason or '; '.join(card.incomplete_reason) or '-'} | {preview_link} |"
            )
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_batch_summary(date_str: str) -> list[dict[str, Any]]:
    json_path = REPORTS_DIR / date_str / "h5_cleaning" / "_batch_summary.json"
    csv_path = REPORTS_DIR / date_str / "h5_cleaning" / "_batch_summary.csv"
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    if not csv_path.exists():
        raise SystemExit(f"batch summary 不存在，请先运行 --all: {json_path}")
    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def cleaned_manifest_row(row: dict[str, Any]) -> dict[str, Any]:
    date_str = str(row["date"])
    brand = str(row["brand"])
    city = str(row["city"])
    bottom_fragment_count = 0
    overlap_fragment_duplicate_count = 0
    brand_text_missing_kept_count = 0
    main_result_bundle_kept_count = 0
    json_path = Path(str(row.get("json_path", "")))
    if json_path.exists():
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        bottom_fragment_count = sum(
            1
            for card in payload.get("cards", [])
            if card.get("action_reason") == "bottom_edge_multi_card_fragment"
            or card.get("exclusion_reason") == "bottom_edge_multi_card_fragment"
        )
        overlap_fragment_duplicate_count = sum(
            1
            for card in payload.get("cards", [])
            if card.get("action_reason") == "overlap_fragment_duplicate"
        )
        brand_text_missing_kept_count = sum(
            1
            for card in payload.get("cards", [])
            if card.get("action") == "keep" and card.get("brand_text_missing_but_main_result_kept")
        )
        main_result_bundle_kept_count = sum(
            1
            for card in payload.get("cards", [])
            if card.get("action") == "keep" and "main_result_bundle_kept" in str(card.get("action_reason", ""))
        )
    return {
        "date": date_str,
        "brand": brand,
        "city": city,
        "delivery_grade": row.get("delivery_grade", ""),
        "source_h5_path": row.get("source_h5_path") or str(source_h5_path_for(date_str, brand, city)),
        "rebuilt_image_path": row.get("rebuilt_image_path", ""),
        "cleaned_output_path": str(cleaned_h5_path_for(date_str, brand, city)),
        "complete_cards_kept": row.get("complete_cards_kept", ""),
        "duplicate_skus_removed": row.get("duplicate_skus_removed", ""),
        "incomplete_cards_excluded": row.get("incomplete_cards_excluded", ""),
        "incomplete_cards_warning": row.get("incomplete_cards_warning", ""),
        "recommendation_cards_excluded": row.get("recommendation_cards_excluded", ""),
        "brand_text_missing_but_main_result_kept": brand_text_missing_kept_count,
        "main_result_bundle_kept": main_result_bundle_kept_count,
        "overlap_fragment_duplicate_excluded": overlap_fragment_duplicate_count,
        "bottom_edge_multi_card_fragment_excluded": bottom_fragment_count,
        "possible_missing_sku": row.get("possible_missing_sku_warnings", ""),
        "status": "pending",
        "warning": "",
    }


def write_cleaned_manifest(date_str: str, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    root = REPORTS_DIR / date_str / "h5_cleaning"
    csv_path = root / "_cleaned_manifest.csv"
    md_path = root / "_cleaned_manifest.md"
    fields = [
        "date",
        "brand",
        "city",
        "delivery_grade",
        "source_h5_path",
        "rebuilt_image_path",
        "cleaned_output_path",
        "complete_cards_kept",
        "duplicate_skus_removed",
        "incomplete_cards_excluded",
        "incomplete_cards_warning",
        "recommendation_cards_excluded",
        "brand_text_missing_but_main_result_kept",
        "main_result_bundle_kept",
        "overlap_fragment_duplicate_excluded",
        "bottom_edge_multi_card_fragment_excluded",
        "possible_missing_sku",
        "status",
        "warning",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    success_rows = [row for row in rows if row["status"] == "success"]
    failed_rows = [row for row in rows if row["status"] == "failed"]
    brand_counts: dict[str, int] = defaultdict(int)
    for row in success_rows:
        brand_counts[str(row["brand"])] += 1
    lines = [
        f"# Cleaned H5 Manifest {date_str}",
        "",
        f"- cleaned 成功数: {len(success_rows)}",
        f"- cleaned 失败数: {len(failed_rows)}",
        f"- cleaned 输出根目录: `{DELIVERABLES_H5_CLEANED_DIR}`",
        "",
        "## 各品牌 cleaned 数量",
        "",
    ]
    if brand_counts:
        for brand, count in sorted(brand_counts.items()):
            lines.append(f"- {brand}: {count}")
    else:
        lines.append("- 无")
    lines.extend(["", "## 明细", ""])
    lines.append("| date | brand | city | grade | status | warning | output |")
    lines.append("|---|---|---|---|---|---|---|")
    for row in rows:
        lines.append(
            f"| {row['date']} | {row['brand']} | {row['city']} | {row['delivery_grade']} | "
            f"{row['status']} | {row['warning'] or '-'} | {row['cleaned_output_path']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path


def generate_cleaned_outputs(date_str: str) -> dict[str, Any]:
    batch_rows = read_batch_summary(date_str)
    manifest_rows: list[dict[str, Any]] = []
    for row in batch_rows:
        if row.get("delivery_grade") != "fixable":
            continue
        manifest_row = cleaned_manifest_row(row)
        rebuilt_path = Path(str(manifest_row["rebuilt_image_path"]))
        output_path = Path(str(manifest_row["cleaned_output_path"]))
        if not rebuilt_path.exists():
            manifest_row["status"] = "failed"
            manifest_row["warning"] = "rebuilt_image_path_missing"
            manifest_rows.append(manifest_row)
            continue
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rebuilt_path, output_path)
            manifest_row["status"] = "success"
        except Exception as exc:
            manifest_row["status"] = "failed"
            manifest_row["warning"] = f"copy_failed:{exc}"
        manifest_rows.append(manifest_row)
    csv_path, md_path = write_cleaned_manifest(date_str, manifest_rows)
    success_rows = [row for row in manifest_rows if row["status"] == "success"]
    failed_rows = [row for row in manifest_rows if row["status"] == "failed"]
    brand_counts: dict[str, int] = defaultdict(int)
    for row in success_rows:
        brand_counts[str(row["brand"])] += 1
    grade_counts: dict[str, int] = defaultdict(int)
    for row in batch_rows:
        grade_counts[str(row.get("delivery_grade", ""))] += 1
    return {
        "manifest_csv_path": str(csv_path),
        "manifest_md_path": str(md_path),
        "cleaned_root": str(DELIVERABLES_H5_CLEANED_DIR),
        "fixable_total": grade_counts.get("fixable", 0),
        "success_count": len(success_rows),
        "failed_count": len(failed_rows),
        "brand_counts": dict(sorted(brand_counts.items())),
        "grade_counts": dict(grade_counts),
        "fail_count": grade_counts.get("fail", 0),
    }


def build_reconstructed_preview(
    *,
    cards: list[CardCandidate],
    decision: dict[str, Any],
    output_path: Path,
) -> str:
    card_map = {card.card_id: card for card in cards}
    kept_ids: list[str] = []
    for group in decision["sku_groups"]:
        selected = group.get("selected_card_id", "")
        if selected:
            kept_ids.append(selected)
    kept_cards = [card_map[card_id] for card_id in kept_ids if card_id in card_map]
    kept_cards.sort(key=lambda card: (card.page_index, card.card_index))
    if not kept_cards:
        return ""

    images: list[Image.Image] = []
    separator_height = 28
    try:
        for card in kept_cards:
            images.append(Image.open(card.preview_path).convert("RGB"))
        width = max(img.width for img in images)
        total_height = sum(img.height for img in images) + separator_height * max(0, len(images) - 1)
        canvas = Image.new("RGB", (width, total_height), "white")
        cursor_y = 0
        for index, img in enumerate(images):
            canvas.paste(img, (0, cursor_y))
            cursor_y += img.height
            if index < len(images) - 1:
                separator = Image.new("RGB", (width, separator_height), (245, 247, 250))
                canvas.paste(separator, (0, cursor_y))
                cursor_y += separator_height
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path)
        canvas.close()
        return str(output_path)
    finally:
        for img in images:
            img.close()


def main() -> int:
    args = parse_args()
    if args.all:
        result = run_batch_analysis(args.date)
        counts = result["counts"]
        print(f"batch_summary_csv={result['csv_path']}")
        print(f"batch_summary_json={result['json_path']}")
        print(f"pass_count={counts.get('pass', 0)}")
        print(f"fixable_count={counts.get('fixable', 0)}")
        print(f"fail_count={counts.get('fail', 0)}")
        print(f"analysis_failures={len(result['failures'])}")
        return 0

    if args.generate_cleaned:
        result = generate_cleaned_outputs(args.date)
        counts = result["grade_counts"]
        print(f"pass_count={counts.get('pass', 0)}")
        print(f"fixable_count={counts.get('fixable', 0)}")
        print(f"fail_count={counts.get('fail', 0)}")
        print(f"cleaned_success_count={result['success_count']}")
        print(f"cleaned_failed_count={result['failed_count']}")
        print(f"brand_counts={json.dumps(result['brand_counts'], ensure_ascii=False)}")
        print(f"cleaned_manifest_csv={result['manifest_csv_path']}")
        print(f"cleaned_manifest_md={result['manifest_md_path']}")
        print(f"cleaned_root={result['cleaned_root']}")
        return 0

    if not args.brand or not args.city:
        raise SystemExit("单样本分析需要提供 --brand 和 --city；全量分析请使用 --all")

    payload = analyze_sample(
        date_str=args.date,
        brand=args.brand,
        city=args.city,
        pages_dir_override=args.pages_dir,
        output_json=args.output_json,
        output_csv=args.output_csv,
        output_md=args.output_md,
    )
    decision = payload["summary"]

    print(f"pages_dir={payload['pages_dir']}")
    print(f"page_count={payload['page_count']}")
    print(f"card_count={payload['card_count']}")
    print(f"delivery_grade={decision['delivery_grade']}")
    print(f"needs_manual_review={str(decision['needs_manual_review']).lower()}")
    print(f"complete_cards_kept={decision['complete_cards_kept']}")
    print(f"incomplete_cards_excluded={decision['incomplete_cards_excluded']}")
    print(f"incomplete_cards_warning={decision['incomplete_cards_warning']}")
    print(f"duplicate_skus_removed={decision['duplicate_skus_removed']}")
    print(f"recommendation_cards_excluded={decision['recommendation_cards_excluded']}")
    print(f"duplicate_sku_groups={decision['duplicate_sku_groups']}")
    print(f"dropped_duplicate_cards={decision['dropped_duplicate_cards']}")
    print(f"dropped_truncated_cards={decision['dropped_truncated_cards']}")
    print(f"preview_root={payload['preview_root']}")
    print(f"output_json={payload['output_json']}")
    print(f"output_csv={payload['output_csv']}")
    print(f"output_md={payload['output_md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
