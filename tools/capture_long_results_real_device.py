#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


@dataclass
class SegmentInfo:
    index: int
    filename: str
    overlap_with_previous: int | None
    score_with_previous: float | None
    crop_bottom: int | None = None
    stop_texts: list[str] | None = None


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def adb_prefix(serial: str | None) -> list[str]:
    prefix = ["adb"]
    if serial:
        prefix.extend(["-s", serial])
    return prefix


def screencap(serial: str | None, output_path: Path) -> None:
    with output_path.open("wb") as f:
        subprocess.run(
            adb_prefix(serial) + ["exec-out", "screencap", "-p"],
            check=True,
            stdout=f,
        )


def screencap_image(serial: str | None, output_path: Path, retries: int = 3) -> Image.Image:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            screencap(serial, output_path)
            with Image.open(output_path) as img:
                img.load()
                return img.convert("RGB")
        except Exception as exc:
            last_error = exc
            output_path.unlink(missing_ok=True)
            time.sleep(0.4 * attempt)
    raise OSError(f"failed to capture a valid screenshot after {retries} attempts: {last_error}")


def swipe(
    serial: str | None,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    duration_ms: int,
) -> None:
    run(
        adb_prefix(serial)
        + [
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        ]
    )


def dump_ui_xml(serial: str | None) -> str:
    dump_path = "/sdcard/pupu_longshot_window.xml"
    run(adb_prefix(serial) + ["shell", "uiautomator", "dump", dump_path], check=False)
    result = run(adb_prefix(serial) + ["shell", "cat", dump_path], check=False)
    return result.stdout or ""


def find_stop_region(xml: str, stop_texts: list[str]) -> tuple[int | None, list[str]]:
    if not xml or not stop_texts:
        return None, []

    best_y: int | None = None
    hits: list[str] = []
    for match in re.finditer(r'(?:text|content-desc)="([^"]*)".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
        text = match.group(1)
        matched = [token for token in stop_texts if token and token in text]
        if not matched:
            continue
        y1 = int(match.group(3))
        hits.extend(token for token in matched if token not in hits)
        best_y = y1 if best_y is None else min(best_y, y1)
    return best_y, hits


def find_text_hits(xml: str, texts: list[str]) -> list[str]:
    if not xml or not texts:
        return []
    return [token for token in texts if token and token in xml]


def normalize_box(box: Any) -> list[list[float]]:
    try:
        return [[float(point[0]), float(point[1])] for point in box]
    except Exception:
        return []


def box_top(box: Any) -> int:
    normalized = normalize_box(box)
    if not normalized:
        return 0
    return int(min(point[1] for point in normalized))


def run_rapidocr(img: Image.Image) -> tuple[list[dict[str, Any]], str]:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except Exception:
        return [], "local_ocr_unavailable"
    try:
        import numpy as np

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
        items.append(
            {
                "bbox": normalize_box(item[0]),
                "text": str(item[1]),
                "confidence": float(item[2]),
            }
        )
    return items, ""


def detect_recommendation_ocr(
    img: Image.Image,
    texts: list[str],
    min_confidence: float,
) -> tuple[int | None, list[str], float, list[dict[str, Any]], str]:
    ocr_items, warning = run_rapidocr(img)
    best_y: int | None = None
    best_hits: list[str] = []
    best_confidence = 0.0
    for item in ocr_items:
        text = str(item["text"])
        confidence = float(item["confidence"])
        matched = [token for token in texts if token and token in text]
        if not matched or confidence < min_confidence:
            continue
        y = box_top(item["bbox"])
        if best_y is None or y < best_y:
            best_y = y
            best_hits = matched
            best_confidence = confidence
    return best_y, best_hits, best_confidence, ocr_items, warning


def detect_recommendation_template(
    img_path: Path,
    template_dir: Path,
    min_confidence: float,
) -> tuple[int | None, str, float, str]:
    try:
        import cv2  # type: ignore
    except Exception:
        return None, "", 0.0, "opencv_unavailable"
    if not template_dir.exists():
        return None, "", 0.0, "template_not_configured"
    templates = sorted(template_dir.glob("*.png"))
    if not templates:
        return None, "", 0.0, "template_not_configured"
    image = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None, "", 0.0, "template_image_read_failed"
    best_y: int | None = None
    best_name = ""
    best_confidence = 0.0
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
        if best_y is None or max_loc[1] < best_y:
            best_y = int(max_loc[1])
            best_name = template_path.stem
            best_confidence = float(max_value)
    return best_y, best_name, best_confidence, "" if best_y is not None else "recommendation_template_not_detected"


def draw_detection_debug(img: Image.Image, bbox: list[list[float]], output_path: Path) -> None:
    if not bbox:
        img.save(output_path)
        return
    from PIL import ImageDraw

    annotated = img.copy()
    draw = ImageDraw.Draw(annotated)
    xs = [point[0] for point in bbox]
    ys = [point[1] for point in bbox]
    draw.rectangle((min(xs), min(ys), max(xs), max(ys)), outline="red", width=6)
    annotated.save(output_path)


def crop_content(img: Image.Image, top: int, bottom: int) -> Image.Image:
    return img.crop((0, top, img.width, bottom))


def compare_identity(a: Image.Image, b: Image.Image) -> float:
    arr_a = np.asarray(a.convert("L"), dtype=np.int16)
    arr_b = np.asarray(b.convert("L"), dtype=np.int16)
    return float(np.mean(np.abs(arr_a - arr_b)))


def overlap_score(
    prev: Image.Image,
    curr: Image.Image,
    overlap_h: int,
    x_left_ratio: float = 0.20,
    x_right_ratio: float = 0.82,
) -> float:
    x1 = int(prev.width * x_left_ratio)
    x2 = int(prev.width * x_right_ratio)
    prev_arr = np.asarray(prev.crop((x1, prev.height - overlap_h, x2, prev.height)).convert("L"), dtype=np.int16)
    curr_arr = np.asarray(curr.crop((x1, 0, x2, overlap_h)).convert("L"), dtype=np.int16)
    return float(np.mean(np.abs(prev_arr - curr_arr)))


def find_best_overlap(
    prev: Image.Image,
    curr: Image.Image,
    min_overlap: int,
    max_overlap: int,
    step: int,
) -> tuple[int, float]:
    best_h = min_overlap
    best_score = float("inf")
    max_h = min(max_overlap, prev.height, curr.height)
    if max_h < min_overlap:
        return max(0, max_h), best_score
    for overlap_h in range(min_overlap, max_h + 1, step):
        score = overlap_score(prev, curr, overlap_h)
        if score < best_score:
            best_h = overlap_h
            best_score = score
    return best_h, best_score


def stitch_segments(
    segment_paths: list[Path],
    output_path: Path,
    top: int,
    bottom: int,
    crop_bottoms: list[int | None],
    segment_stop_texts: list[list[str]],
    min_overlap: int,
    max_overlap: int,
    step: int,
) -> list[SegmentInfo]:
    images = [Image.open(path).convert("RGB") for path in segment_paths]
    effective_bottoms = [
        min(bottom, crop_bottom) if crop_bottom is not None else bottom
        for crop_bottom in crop_bottoms
    ]
    content_images = [
        crop_content(img, top, max(top + 1, effective_bottoms[index]))
        for index, img in enumerate(images)
    ]

    first_bottom = max(top + 1, effective_bottoms[0])
    pieces: list[Image.Image] = [images[0].crop((0, 0, images[0].width, first_bottom))]
    segment_infos: list[SegmentInfo] = [
        SegmentInfo(
            index=0,
            filename=segment_paths[0].name,
            overlap_with_previous=None,
            score_with_previous=None,
            crop_bottom=crop_bottoms[0],
            stop_texts=segment_stop_texts[0] or None,
        )
    ]

    prev_content = content_images[0]
    for index in range(1, len(images)):
        curr_content = content_images[index]
        overlap_h, score = find_best_overlap(
            prev_content,
            curr_content,
            min(min_overlap, max(1, curr_content.height - 1)),
            min(max_overlap, curr_content.height, prev_content.height),
            step,
        )
        effective_bottom = max(top + overlap_h + 1, effective_bottoms[index])
        append_part = images[index].crop((0, top + overlap_h, images[index].width, effective_bottom))
        pieces.append(append_part)
        segment_infos.append(
            SegmentInfo(
                index=index,
                filename=segment_paths[index].name,
                overlap_with_previous=overlap_h,
                score_with_previous=round(score, 3),
                crop_bottom=crop_bottoms[index],
                stop_texts=segment_stop_texts[index] or None,
            )
        )
        prev_content = curr_content

    total_height = sum(piece.height for piece in pieces)
    width = pieces[0].width
    canvas = Image.new("RGB", (width, total_height), "white")
    cursor_y = 0
    for piece in pieces:
        canvas.paste(piece, (0, cursor_y))
        cursor_y += piece.height
    canvas.save(output_path)
    return segment_infos


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture result-page screenshots from the current Pupu results page.")
    parser.add_argument("--serial", default="", help="adb serial; empty means default device")
    parser.add_argument("--output-dir", required=True, help="directory to save raw segments and long image")
    parser.add_argument("--output-name", required=True, help="final long image filename")
    parser.add_argument(
        "--output-mode",
        choices=["stitch", "pages"],
        default="stitch",
        help="stitch saves one long image; pages saves ordered viewport screenshots without stitching",
    )
    parser.add_argument("--max-shots", type=int, default=10)
    parser.add_argument("--pause-sec", type=float, default=1.4)
    parser.add_argument("--swipe-x", type=int, default=540)
    parser.add_argument("--swipe-start-y", type=int, default=1880)
    parser.add_argument("--swipe-end-y", type=int, default=720)
    parser.add_argument("--swipe-duration-ms", type=int, default=260)
    parser.add_argument("--content-top", type=int, default=300, help="top of scrollable results area")
    parser.add_argument("--content-bottom", type=int, default=2180, help="bottom of scrollable results area")
    parser.add_argument("--min-overlap", type=int, default=500)
    parser.add_argument("--max-overlap", type=int, default=1400)
    parser.add_argument("--overlap-step", type=int, default=10)
    parser.add_argument("--identity-threshold", type=float, default=3.0, help="stop when swipe no longer changes the page")
    parser.add_argument("--bottom-stable-rounds", type=int, default=2, help="consecutive unchanged swipes needed to confirm bottom")
    parser.add_argument(
        "--bottom-text",
        action="append",
        default=[],
        help="text that indicates the result page reached the bottom",
    )
    parser.add_argument(
        "--stop-text",
        action="append",
        default=[],
        help="stop long screenshot when UIAutomator text/content-desc contains this token",
    )
    parser.add_argument("--ocr-min-confidence", type=float, default=0.75)
    parser.add_argument("--template-dir", default="")
    parser.add_argument("--template-min-confidence", type=float, default=0.80)
    parser.add_argument(
        "--recommendation-stop-mode",
        choices=["crop_above", "keep_current_viewport"],
        default="crop_above",
    )
    parser.add_argument("--debug-recommendation", action="store_true")
    parser.add_argument("--debug-dir", default="")
    parser.add_argument("--stop-crop-margin", type=int, default=24, help="pixels to keep above stop text when cropping")
    parser.add_argument(
        "--min-stop-crop-height",
        type=int,
        default=520,
        help="discard a stop segment if the crop would contain less than this many content pixels",
    )
    args = parser.parse_args()

    serial = args.serial or None
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_paths: list[Path] = []
    crop_bottoms: list[int | None] = []
    segment_stop_texts: list[list[str]] = []
    prev_content: Image.Image | None = None
    capture_stop_reason = "max_shots_reached"
    reached_page_bottom = False
    bottom_detection_method = ""
    reached_recommendation_section = False
    recommendation_detect_method = ""
    recommendation_detect_text = ""
    recommendation_detect_confidence = ""
    recommendation_detect_shot_index = ""
    recommendation_detect_bbox: list[list[float]] = []
    recommendation_warnings: list[str] = []
    unchanged_rounds = 0
    debug_dir = Path(args.debug_dir).resolve() if args.debug_recommendation and args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    for index in range(args.max_shots):
        path = output_dir / f"segment_{index:02d}.png"
        current_img = screencap_image(serial, path)
        raw_paths.append(path)
        if debug_dir:
            shutil.copy2(path, debug_dir / f"shot_{index + 1:03d}.png")
        current_xml = dump_ui_xml(serial)
        bottom_hits = find_text_hits(current_xml, args.bottom_text)
        stop_y, stop_hits = find_stop_region(current_xml, args.stop_text)
        detected_method = "uiautomator" if stop_y is not None else ""
        detected_confidence = "1.000" if stop_y is not None else ""
        detected_bbox: list[list[float]] = []
        if stop_y is None:
            ocr_y, ocr_hits, ocr_confidence, ocr_items, ocr_warning = detect_recommendation_ocr(
                current_img,
                args.stop_text,
                args.ocr_min_confidence,
            )
            if debug_dir:
                (debug_dir / f"shot_{index + 1:03d}_ocr.json").write_text(
                    json.dumps(ocr_items, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            if ocr_warning and ocr_warning not in recommendation_warnings:
                recommendation_warnings.append(ocr_warning)
            if ocr_y is not None:
                stop_y = ocr_y
                stop_hits = ocr_hits
                detected_method = "ocr_current_viewport"
                detected_confidence = f"{ocr_confidence:.3f}"
                for item in ocr_items:
                    if any(token in str(item.get("text", "")) for token in ocr_hits):
                        detected_bbox = item.get("bbox") or []
                        break
            elif args.template_dir:
                template_y, template_name, template_confidence, template_warning = detect_recommendation_template(
                    path,
                    Path(args.template_dir).resolve(),
                    args.template_min_confidence,
                )
                if template_warning and template_warning not in recommendation_warnings:
                    recommendation_warnings.append(template_warning)
                if template_y is not None:
                    stop_y = template_y
                    stop_hits = [template_name]
                    detected_method = "template_current_viewport"
                    detected_confidence = f"{template_confidence:.3f}"
        crop_bottom: int | None = None
        if stop_y is not None:
            if args.recommendation_stop_mode == "crop_above":
                crop_bottom = max(args.content_top, min(args.content_bottom, stop_y - args.stop_crop_margin))
                # For later viewports the final stitched image already contains prior
                # product cards, so a recommendation title near the top of the
                # current screen is still a safe crop point.
                if index > 0 and crop_bottom <= args.content_top + 80:
                    crop_bottom = None
                    stop_hits = []
                    if "crop_position_unsafe" not in recommendation_warnings:
                        recommendation_warnings.append("crop_position_unsafe")
                if index == 0 and crop_bottom - args.content_top < args.min_stop_crop_height:
                    crop_bottom = None
                    stop_hits = []
            if stop_hits:
                reached_recommendation_section = True
                recommendation_detect_method = detected_method
                recommendation_detect_text = ",".join(stop_hits)
                recommendation_detect_confidence = detected_confidence
                recommendation_detect_shot_index = str(index + 1)
                recommendation_detect_bbox = detected_bbox
                if debug_dir:
                    if detected_bbox:
                        draw_detection_debug(current_img, detected_bbox, debug_dir / "recommendation_detected.png")
                    else:
                        current_img.save(debug_dir / "recommendation_detected.png")

        crop_bottoms.append(crop_bottom)
        segment_stop_texts.append(stop_hits)
        effective_bottom = crop_bottom if crop_bottom is not None else args.content_bottom
        current_content = crop_content(current_img, args.content_top, effective_bottom)

        if stop_hits:
            capture_stop_reason = (
                "recommendation_detected_keep_current_viewport"
                if args.recommendation_stop_mode == "keep_current_viewport"
                else "recommendation_text_detected"
            )
            break

        if prev_content is not None:
            delta = compare_identity(prev_content, current_content)
            if delta <= args.identity_threshold:
                path.unlink(missing_ok=True)
                raw_paths.pop()
                crop_bottoms.pop()
                segment_stop_texts.pop()
                unchanged_rounds += 1
                if unchanged_rounds >= args.bottom_stable_rounds:
                    capture_stop_reason = "page_bottom_identity"
                    reached_page_bottom = True
                    bottom_detection_method = "two_unchanged_screens"
                    break
                if index < args.max_shots - 1:
                    swipe(
                        serial,
                        args.swipe_x,
                        args.swipe_start_y,
                        args.swipe_x,
                        args.swipe_end_y,
                        args.swipe_duration_ms,
                    )
                    time.sleep(args.pause_sec)
                continue
            unchanged_rounds = 0

        prev_content = current_content
        if bottom_hits:
            capture_stop_reason = "bottom_text_detected"
            reached_page_bottom = True
            bottom_detection_method = "bottom_text:" + ",".join(bottom_hits)
            break

        if index < args.max_shots - 1:
            swipe(
                serial,
                args.swipe_x,
                args.swipe_start_y,
                args.swipe_x,
                args.swipe_end_y,
                args.swipe_duration_ms,
            )
            time.sleep(args.pause_sec)

    if not raw_paths:
        raise SystemExit("No screenshots captured.")

    long_image_path = output_dir / args.output_name
    if args.output_mode == "pages":
        segment_infos = [
            SegmentInfo(
                index=index,
                filename=path.name,
                overlap_with_previous=None,
                score_with_previous=None,
                crop_bottom=None,
                stop_texts=segment_stop_texts[index] or None,
            )
            for index, path in enumerate(raw_paths)
        ]
    else:
        segment_infos = stitch_segments(
            raw_paths,
            long_image_path,
            top=args.content_top,
            bottom=args.content_bottom,
            crop_bottoms=crop_bottoms,
            segment_stop_texts=segment_stop_texts,
            min_overlap=args.min_overlap,
            max_overlap=args.max_overlap,
            step=args.overlap_step,
        )

    metadata_path = output_dir / f"{Path(args.output_name).stem}_segments.json"
    metadata_path.write_text(
        json.dumps(
            {
                "segments": [asdict(item) for item in segment_infos],
                "output_mode": args.output_mode,
                "capture_stop_reason": capture_stop_reason,
                "reached_page_bottom": reached_page_bottom,
                "reached_recommendation_section": reached_recommendation_section,
                "recommendation_detect_method": recommendation_detect_method,
                "recommendation_detect_text": recommendation_detect_text,
                "recommendation_detect_confidence": recommendation_detect_confidence,
                "recommendation_detect_shot_index": recommendation_detect_shot_index,
                "recommendation_detect_bbox": recommendation_detect_bbox,
                "recommendation_warnings": recommendation_warnings,
                "bottom_detection_method": bottom_detection_method,
                "shot_count": len(raw_paths),
                "max_shots": args.max_shots,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.output_mode == "pages":
        print(f"Saved page screenshots: {output_dir}")
    else:
        print(f"Saved long screenshot: {long_image_path}")
    print(f"Saved metadata: {metadata_path}")
    print(f"Captured segments: {len(raw_paths)}")


if __name__ == "__main__":
    main()
