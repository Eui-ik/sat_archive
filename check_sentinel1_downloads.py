#!/usr/bin/env python3
import argparse
import csv
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


NAME_RE = re.compile(
    r"^(?P<mission>S1[A-Z])_"
    r"(?P<mode>[A-Z]{2})_"
    r"(?P<product>SLC)__"
    r"(?P<level>\d)"
    r"(?P<class>[A-Z])"
    r"(?P<pol>[A-Z]{2})_"
    r"(?P<start>\d{8}T\d{6})_"
    r"(?P<stop>\d{8}T\d{6})_"
    r"(?P<orbit>\d{6})_"
    r"(?P<datatake>[0-9A-F]{6})_"
    r"(?P<unique>[0-9A-F]{4})\.zip$"
)


def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def parse_datetime(value):
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


def quick_zip_status(path):
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            if not infos:
                return "empty-zip", 0, ""
            roots = {info.filename.split("/", 1)[0] for info in infos if info.filename}
            return "ok", len(infos), ",".join(sorted(list(roots))[:3])
    except zipfile.BadZipFile:
        return "bad-zip", 0, ""
    except Exception as error:
        return f"error:{type(error).__name__}", 0, ""


def deep_zip_status(path):
    try:
        with zipfile.ZipFile(path) as zf:
            bad_member = zf.testzip()
            if bad_member:
                return f"crc-failed:{bad_member}"
            return "crc-ok"
    except zipfile.BadZipFile:
        return "bad-zip"
    except Exception as error:
        return f"error:{type(error).__name__}"


def build_rows(directory, test_zip):
    rows = []
    for path in sorted(directory.glob("*.zip")):
        match = NAME_RE.match(path.name)
        stat = path.stat()
        row = {
            "file": path.name,
            "size_bytes": stat.st_size,
            "size": human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "name_valid": bool(match),
            "mission": "",
            "start": "",
            "stop": "",
            "date": "",
            "orbit": "",
            "datatake": "",
            "zip_status": "",
            "zip_entries": "",
            "zip_roots": "",
            "crc_status": "",
        }

        if match:
            data = match.groupdict()
            start_dt = parse_datetime(data["start"])
            stop_dt = parse_datetime(data["stop"])
            row.update(
                {
                    "mission": data["mission"],
                    "start": start_dt.isoformat(),
                    "stop": stop_dt.isoformat(),
                    "date": start_dt.date().isoformat(),
                    "orbit": data["orbit"],
                    "datatake": data["datatake"],
                }
            )

        status, entries, roots = quick_zip_status(path)
        row["zip_status"] = status
        row["zip_entries"] = entries
        row["zip_roots"] = roots

        if test_zip:
            print(f"CRC testing {path.name} ...", flush=True)
            row["crc_status"] = deep_zip_status(path)

        rows.append(row)

    return rows


def summarize(rows, directory):
    generated_report_names = {
        "sentinel1_download_report.txt",
        "sentinel1_download_inventory.csv",
    }
    zip_files = list(directory.glob("*.zip"))
    part_files = list(directory.glob("*.part"))
    other_files = [
        path for path in directory.iterdir()
        if path.is_file() and path.suffix not in {".zip", ".part"}
        and path.name not in generated_report_names
    ]
    total_size = sum(row["size_bytes"] for row in rows)
    valid_rows = [row for row in rows if row["name_valid"]]
    bad_name_rows = [row for row in rows if not row["name_valid"]]
    bad_zip_rows = [row for row in rows if row["zip_status"] != "ok"]
    zero_size_rows = [row for row in rows if row["size_bytes"] == 0]
    small_rows = [row for row in rows if 0 < row["size_bytes"] < 1024**3]

    by_file = Counter(row["file"] for row in rows)
    duplicate_files = [name for name, count in by_file.items() if count > 1]

    by_start = defaultdict(list)
    by_date = defaultdict(list)
    by_mission = Counter()
    by_month = Counter()

    for row in valid_rows:
        by_start[row["start"]].append(row)
        by_date[row["date"]].append(row)
        by_mission[row["mission"]] += 1
        by_month[row["date"][:7]] += 1

    duplicate_starts = {
        start: values for start, values in by_start.items()
        if len(values) > 1
    }
    multi_scene_dates = {
        date: values for date, values in by_date.items()
        if len(values) > 1
    }

    sorted_dates = sorted(by_date)
    gaps = []
    for previous, current in zip(sorted_dates, sorted_dates[1:]):
        prev_dt = datetime.strptime(previous, "%Y-%m-%d")
        cur_dt = datetime.strptime(current, "%Y-%m-%d")
        delta = (cur_dt - prev_dt).days
        if delta > 12:
            gaps.append((previous, current, delta))

    return {
        "zip_count": len(zip_files),
        "part_count": len(part_files),
        "other_count": len(other_files),
        "total_size": total_size,
        "valid_count": len(valid_rows),
        "bad_names": bad_name_rows,
        "bad_zips": bad_zip_rows,
        "zero_size": zero_size_rows,
        "small_files": small_rows,
        "duplicate_files": duplicate_files,
        "duplicate_starts": duplicate_starts,
        "multi_scene_dates": multi_scene_dates,
        "by_mission": by_mission,
        "by_month": by_month,
        "first_date": sorted_dates[0] if sorted_dates else "",
        "last_date": sorted_dates[-1] if sorted_dates else "",
        "gaps": gaps,
        "part_files": part_files,
        "other_files": other_files,
    }


def write_csv(rows, csv_path):
    fields = [
        "file", "size_bytes", "size", "modified", "name_valid", "mission",
        "start", "stop", "date", "orbit", "datatake", "zip_status",
        "zip_entries", "zip_roots", "crc_status",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def format_summary(summary):
    lines = []
    lines.append("Sentinel-1 download check")
    lines.append("=========================")
    lines.append(f"Zip files: {summary['zip_count']}")
    lines.append(f"Partial .part files: {summary['part_count']}")
    lines.append(f"Other files: {summary['other_count']}")
    lines.append(f"Total zip size: {human_size(summary['total_size'])}")
    lines.append(f"Valid Sentinel-1 names: {summary['valid_count']}")
    lines.append(f"Date range: {summary['first_date']} to {summary['last_date']}")
    lines.append("")

    lines.append("Missions")
    for mission, count in sorted(summary["by_mission"].items()):
        lines.append(f"- {mission}: {count}")
    lines.append("")

    lines.append("Monthly counts")
    for month, count in sorted(summary["by_month"].items()):
        lines.append(f"- {month}: {count}")
    lines.append("")

    lines.append("Issues")
    issue_count = 0
    checks = [
        ("Bad file names", summary["bad_names"]),
        ("Bad zip headers", summary["bad_zips"]),
        ("Zero-size files", summary["zero_size"]),
        ("Files smaller than 1 GB", summary["small_files"]),
        ("Duplicate file names", summary["duplicate_files"]),
        ("Duplicate acquisition start times", summary["duplicate_starts"]),
        ("Partial .part files", summary["part_files"]),
    ]
    for label, values in checks:
        count = len(values)
        if count:
            issue_count += count
            lines.append(f"- {label}: {count}")
            if isinstance(values, dict):
                sample = list(values.items())[:10]
                for key, rows in sample:
                    names = ", ".join(row["file"] for row in rows)
                    lines.append(f"  - {key}: {names}")
            else:
                for value in list(values)[:10]:
                    name = value["file"] if isinstance(value, dict) else Path(value).name
                    lines.append(f"  - {name}")
    if issue_count == 0:
        lines.append("- No basic file, zip-header, duplicate, or partial-file issues found.")
    lines.append("")

    lines.append("Dates with multiple scenes")
    if summary["multi_scene_dates"]:
        for date, rows in sorted(summary["multi_scene_dates"].items()):
            names = ", ".join(row["file"] for row in rows)
            lines.append(f"- {date}: {len(rows)} scenes")
            lines.append(f"  {names}")
    else:
        lines.append("- None")
    lines.append("")

    lines.append("Date gaps over 12 days")
    if summary["gaps"]:
        for previous, current, delta in summary["gaps"]:
            lines.append(f"- {previous} -> {current}: {delta} days")
    else:
        lines.append("- None")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Check Sentinel-1 zip downloads for basic integrity and coverage."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="sentinel1_jeju",
        help="Directory containing Sentinel-1 zip files.",
    )
    parser.add_argument(
        "--test-zip",
        action="store_true",
        help="Run full CRC checks. This reads all zip contents and can take a long time.",
    )
    args = parser.parse_args()

    directory = Path(args.directory).expanduser().resolve()
    if not directory.exists():
        raise SystemExit(f"Directory does not exist: {directory}")

    rows = build_rows(directory, args.test_zip)
    summary = summarize(rows, directory)

    report_path = directory / "sentinel1_download_report.txt"
    csv_path = directory / "sentinel1_download_inventory.csv"

    report = format_summary(summary)
    report_path.write_text(report + "\n", encoding="utf-8")
    write_csv(rows, csv_path)

    print(report)
    print("")
    print(f"Report written to: {report_path}")
    print(f"Inventory CSV written to: {csv_path}")


if __name__ == "__main__":
    main()
