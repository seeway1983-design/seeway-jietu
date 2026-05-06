#!/usr/bin/env python3
"""Create local brand ZIP delivery packages from cleaned H5 images."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass(frozen=True)
class EnabledItem:
    name: str
    alias: str


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def load_enabled_cities() -> list[EnabledItem]:
    cities = []
    for item in read_json(CONFIG_DIR / "cities.json"):
        if item.get("enabled") is True:
            city = str(item["city"])
            cities.append(EnabledItem(name=city, alias=str(item.get("city_alias") or city.removesuffix("市"))))
    return cities


def load_enabled_brands() -> list[str]:
    return [str(item["brand"]) for item in read_json(CONFIG_DIR / "brands.json") if item.get("enabled") is True]


def parse_date(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"invalid --date {value!r}; expected YYYY-MM-DD") from exc


def zip_date_label(date: str) -> str:
    dt = parse_date(date)
    return f"{dt.year}年{dt.month}月{dt.day}日"


def image_date_label(date: str) -> str:
    return parse_date(date).strftime("%Y.%m.%d")


def bool_text(value: bool) -> str:
    return "true" if value else "false"


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def file_size_mb(path: Path) -> str:
    if not path.exists():
        return ""
    return f"{path.stat().st_size / 1024 / 1024:.3f}"


def load_gatekeeper(date: str, report_dir: Path, errors: list[str]) -> dict[str, Any] | None:
    gatekeeper_json = report_dir / "_delivery_gatekeeper_report.json"
    if not gatekeeper_json.exists():
        errors.append("gatekeeper_report_missing")
        return None
    try:
        data = read_json(gatekeeper_json)
    except json.JSONDecodeError as exc:
        errors.append(f"gatekeeper_report_invalid_json:{exc}")
        return None
    if data.get("date") != date:
        errors.append(f"gatekeeper_date_mismatch:{data.get('date')}")
    if data.get("overall_gate_result") != "pass":
        errors.append(f"gatekeeper_not_pass:{data.get('overall_gate_result')}")
    return data


def validate_preflight(
    date: str,
    enabled_brands: list[str],
    enabled_cities: list[EnabledItem],
    expected_total: int,
) -> tuple[list[dict[str, str]], dict[tuple[str, str], dict[str, str]], list[str]]:
    errors: list[str] = []
    report_dir = PROJECT_ROOT / "reports" / date / "h5_cleaning"
    cleaned_root = PROJECT_ROOT / "deliverables_h5_cleaned"

    load_gatekeeper(date, report_dir, errors)

    manifest_path = report_dir / "_cleaned_manifest.csv"
    quality_path = report_dir / "_cleaned_quality_check.csv"
    rerun_path = report_dir / "_rerun_required.csv"

    if not manifest_path.exists():
        errors.append("cleaned_manifest_missing")
        manifest_rows: list[dict[str, str]] = []
    else:
        manifest_rows = read_csv(manifest_path)

    if not quality_path.exists():
        errors.append("quality_check_missing")
        quality_rows: list[dict[str, str]] = []
    else:
        quality_rows = read_csv(quality_path)

    if not rerun_path.exists():
        errors.append("rerun_required_missing")
        rerun_rows: list[dict[str, str]] = []
    else:
        rerun_rows = read_csv(rerun_path)

    enabled_pairs = {(brand, city.name) for brand in enabled_brands for city in enabled_cities}
    manifest_pairs = {(row.get("brand", ""), row.get("city", "")) for row in manifest_rows}

    if len(manifest_rows) != expected_total:
        errors.append(f"cleaned_manifest_row_count_mismatch:{len(manifest_rows)}!={expected_total}")
    missing_pairs = sorted(enabled_pairs - manifest_pairs)
    extra_pairs = sorted(manifest_pairs - enabled_pairs)
    if missing_pairs:
        errors.append(f"cleaned_manifest_missing_pairs:{missing_pairs}")
    if extra_pairs:
        errors.append(f"cleaned_manifest_extra_pairs:{extra_pairs}")

    quality_pairs = {(row.get("brand", ""), row.get("city", "")) for row in quality_rows}
    if len(quality_rows) != expected_total:
        errors.append(f"quality_check_row_count_mismatch:{len(quality_rows)}!={expected_total}")
    if enabled_pairs - quality_pairs:
        errors.append(f"quality_check_missing_pairs:{sorted(enabled_pairs - quality_pairs)}")
    bad_quality = [
        f"{row.get('brand')}/{row.get('city')}:{row.get('quality_result')}"
        for row in quality_rows
        if row.get("quality_result") != "pass"
    ]
    if bad_quality:
        errors.append(f"quality_check_not_all_pass:{bad_quality}")

    nonempty_rerun = [row for row in rerun_rows if any((value or "").strip() for value in row.values())]
    if nonempty_rerun:
        errors.append(f"rerun_required_not_empty:{len(nonempty_rerun)}")

    brand_counts = Counter(row.get("brand", "") for row in manifest_rows)
    city_counts = Counter(row.get("city", "") for row in manifest_rows)
    for brand in enabled_brands:
        if brand_counts[brand] != len(enabled_cities):
            errors.append(f"brand_cleaned_count_mismatch:{brand}:{brand_counts[brand]}!={len(enabled_cities)}")
    for city in enabled_cities:
        if city_counts[city.name] != len(enabled_brands):
            errors.append(f"city_cleaned_count_mismatch:{city.name}:{city_counts[city.name]}!={len(enabled_brands)}")

    rows_by_pair: dict[tuple[str, str], dict[str, str]] = {}
    for row in manifest_rows:
        brand = row.get("brand", "")
        city = row.get("city", "")
        cleaned_path_text = row.get("cleaned_output_path", "")
        cleaned_path = Path(cleaned_path_text)
        if not cleaned_path.is_absolute():
            cleaned_path = PROJECT_ROOT / cleaned_path
        row["cleaned_output_path"] = str(cleaned_path)
        rows_by_pair[(brand, city)] = row

        if (brand, city) not in enabled_pairs:
            continue
        if not is_relative_to(cleaned_path, cleaned_root):
            errors.append(f"cleaned_path_not_under_cleaned_root:{brand}/{city}:{cleaned_path}")
        if str(cleaned_path).find("/deliverables_h5/") >= 0:
            errors.append(f"cleaned_path_points_to_raw_h5:{brand}/{city}:{cleaned_path}")
        if not cleaned_path.exists():
            errors.append(f"cleaned_file_missing:{brand}/{city}:{cleaned_path}")
        elif cleaned_path.stat().st_size == 0:
            errors.append(f"cleaned_file_zero_bytes:{brand}/{city}:{cleaned_path}")

    return manifest_rows, rows_by_pair, errors


def build_manifest_rows(
    date: str,
    enabled_brands: list[str],
    enabled_cities: list[EnabledItem],
    rows_by_pair: dict[tuple[str, str], dict[str, str]],
    package_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for brand in enabled_brands:
        zip_name = f"{brand}巡价{zip_date_label(date)}.zip"
        zip_path = package_dir / "by_brand" / zip_name
        for city in enabled_cities:
            source = rows_by_pair.get((brand, city.name), {}).get("cleaned_output_path", "")
            source_path = Path(source) if source else Path()
            inner_name = f"{brand}-{city.alias}-{image_date_label(date)}.png"
            exists = bool(source) and source_path.exists()
            status = "ready"
            error = ""
            if not source:
                status = "failed"
                error = "missing_cleaned_manifest_row"
            elif not exists:
                status = "failed"
                error = "missing_cleaned_image"
            elif source_path.stat().st_size == 0:
                status = "failed"
                error = "zero_byte_cleaned_image"

            rows.append(
                {
                    "date": date,
                    "brand": brand,
                    "city": city.name,
                    "source_cleaned_path": str(source_path) if source else "",
                    "zip_name": zip_name,
                    "zip_path": str(zip_path),
                    "zip_inner_filename": inner_name,
                    "source_file_exists": bool_text(exists),
                    "source_file_size_mb": file_size_mb(source_path) if exists else "",
                    "package_status": status,
                    "error_message": error,
                }
            )
    return rows


def write_manifest_md(path: Path, date: str, rows: list[dict[str, Any]], summary: dict[str, Any]) -> None:
    brand_counts = Counter(row["brand"] for row in rows if row["package_status"] == "packaged")
    lines = [
        f"# {date} 本地品牌 ZIP 交付 Manifest",
        "",
        f"- gatekeeper_status: {summary['gatekeeper_status']}",
        f"- package_status: {summary['package_status']}",
        f"- expected_total: {summary['expected_total']}",
        f"- total_images_packaged: {summary['total_images_packaged']}",
        f"- total_zip_count: {summary['total_zip_count']}",
        "",
        "## 品牌 ZIP",
        "",
    ]
    for brand, zip_path in summary.get("zip_paths", {}).items():
        lines.append(f"- {brand}: {zip_path}，图片数 {brand_counts.get(brand, 0)}")
    if summary.get("errors"):
        lines.extend(["", "## 错误", ""])
        lines.extend(f"- {error}" for error in summary["errors"])
    lines.extend(["", "## 图片清单", ""])
    lines.append("| brand | city | status | zip_inner_filename | source |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            f"| {row['brand']} | {row['city']} | {row['package_status']} | "
            f"{row['zip_inner_filename']} | {row['source_cleaned_path']} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_blocked_summary(
    date: str,
    package_dir: Path,
    enabled_city_count: int,
    enabled_brand_count: int,
    expected_total: int,
    errors: list[str],
) -> None:
    manifest_dir = package_dir / "manifest"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "date": date,
        "enabled_city_count": enabled_city_count,
        "enabled_brand_count": enabled_brand_count,
        "expected_total": expected_total,
        "total_images_packaged": 0,
        "total_zip_count": 0,
        "brands": [],
        "cities": [],
        "gatekeeper_status": "fail",
        "package_status": "blocked_by_gatekeeper" if any("gatekeeper" in e for e in errors) else "failed",
        "errors": errors,
    }
    (manifest_dir / "package_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def package_brand_zip(
    brand: str,
    rows: list[dict[str, Any]],
    overwrite_package: bool,
) -> tuple[str, list[dict[str, Any]], str]:
    zip_path = Path(rows[0]["zip_path"])
    failed_rows = [row for row in rows if row["package_status"] == "failed"]
    if failed_rows:
        for row in rows:
            if row["package_status"] != "failed":
                row["package_status"] = "skipped_brand_has_missing_image"
        return "failed", rows, f"{brand}:missing_cleaned_image"

    if zip_path.exists() and not overwrite_package:
        for row in rows:
            row["package_status"] = "skipped_existing_package"
        return "skipped", rows, ""

    zip_path.parent.mkdir(parents=True, exist_ok=True)
    temp_zip_path = zip_path.with_suffix(zip_path.suffix + ".tmp")
    if temp_zip_path.exists():
        temp_zip_path.unlink()

    with zipfile.ZipFile(temp_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            zf.write(row["source_cleaned_path"], arcname=row["zip_inner_filename"])
            row["package_status"] = "packaged"
    temp_zip_path.replace(zip_path)
    return "packaged", rows, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Package daily cleaned H5 images into local brand ZIP files.")
    parser.add_argument("--date", required=True, help="Target date in YYYY-MM-DD format.")
    parser.add_argument("--overwrite-package", action="store_true", help="Overwrite existing ZIP packages.")
    args = parser.parse_args()

    date = args.date
    parse_date(date)

    enabled_cities = load_enabled_cities()
    enabled_brands = load_enabled_brands()
    enabled_city_count = len(enabled_cities)
    enabled_brand_count = len(enabled_brands)
    expected_total = enabled_city_count * enabled_brand_count
    package_dir = PROJECT_ROOT / "delivery_packages" / date
    manifest_dir = package_dir / "manifest"

    manifest_rows, rows_by_pair, errors = validate_preflight(date, enabled_brands, enabled_cities, expected_total)
    if errors:
        write_blocked_summary(date, package_dir, enabled_city_count, enabled_brand_count, expected_total, errors)
        print("package_status=blocked_by_gatekeeper" if any("gatekeeper" in e for e in errors) else "package_status=failed")
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    delivery_rows = build_manifest_rows(date, enabled_brands, enabled_cities, rows_by_pair, package_dir)
    rows_by_brand: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in delivery_rows:
        rows_by_brand[row["brand"]].append(row)

    package_errors: list[str] = []
    zip_paths: dict[str, str] = {}
    brand_package_status: dict[str, str] = {}
    for brand in enabled_brands:
        status, updated_rows, error = package_brand_zip(brand, rows_by_brand[brand], args.overwrite_package)
        rows_by_brand[brand] = updated_rows
        brand_package_status[brand] = status
        zip_path = Path(updated_rows[0]["zip_path"])
        if zip_path.exists():
            zip_paths[brand] = str(zip_path)
        if error:
            package_errors.append(error)

    delivery_rows = [row for brand in enabled_brands for row in rows_by_brand[brand]]
    total_images_packaged = sum(1 for row in delivery_rows if row["package_status"] == "packaged")
    total_zip_count = sum(1 for brand in enabled_brands if Path(rows_by_brand[brand][0]["zip_path"]).exists())
    skipped_count = sum(1 for row in delivery_rows if row["package_status"] == "skipped_existing_package")
    failed_count = sum(1 for row in delivery_rows if row["package_status"] in {"failed", "skipped_brand_has_missing_image"})

    if package_errors or failed_count:
        package_status = "failed"
    elif skipped_count == len(delivery_rows):
        package_status = "skipped_existing_package"
    else:
        package_status = "success"

    fieldnames = [
        "date",
        "brand",
        "city",
        "source_cleaned_path",
        "zip_name",
        "zip_path",
        "zip_inner_filename",
        "source_file_exists",
        "source_file_size_mb",
        "package_status",
        "error_message",
    ]
    manifest_csv = manifest_dir / "delivery_manifest.csv"
    manifest_md = manifest_dir / "delivery_manifest.md"
    summary_json = manifest_dir / "package_summary.json"
    write_csv(manifest_csv, delivery_rows, fieldnames)

    summary = {
        "date": date,
        "enabled_city_count": enabled_city_count,
        "enabled_brand_count": enabled_brand_count,
        "expected_total": expected_total,
        "total_images_packaged": total_images_packaged,
        "total_zip_count": total_zip_count,
        "brands": enabled_brands,
        "cities": [city.name for city in enabled_cities],
        "gatekeeper_status": "pass",
        "package_status": package_status,
        "brand_package_status": brand_package_status,
        "zip_paths": zip_paths,
        "errors": package_errors,
    }
    manifest_dir.mkdir(parents=True, exist_ok=True)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_manifest_md(manifest_md, date, delivery_rows, {**summary, "expected_total": expected_total})

    print(f"package_status={package_status}")
    print(f"enabled_city_count={enabled_city_count}")
    print(f"enabled_brand_count={enabled_brand_count}")
    print(f"expected_total={expected_total}")
    print(f"total_images_packaged={total_images_packaged}")
    print(f"total_zip_count={total_zip_count}")
    print(f"delivery_manifest_csv={manifest_csv}")
    print(f"delivery_manifest_md={manifest_md}")
    print(f"package_summary_json={summary_json}")
    return 0 if package_status in {"success", "skipped_existing_package"} else 1


if __name__ == "__main__":
    sys.exit(main())
