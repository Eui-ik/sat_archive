#!/usr/bin/env python3
import argparse
import shutil
import subprocess
import zipfile
from pathlib import Path


def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def zip_info(path):
    with zipfile.ZipFile(path) as zf:
        infos = zf.infolist()
        roots = sorted(
            {
                info.filename.split("/", 1)[0]
                for info in infos
                if info.filename and "/" in info.filename
            }
        )
        total_uncompressed = sum(info.file_size for info in infos)
    return roots, total_uncompressed


def extract_zip(zip_path, out_dir, force=False):
    roots, uncompressed_size = zip_info(zip_path)
    safe_roots = [root for root in roots if root.endswith(".SAFE")]

    if not safe_roots:
        raise RuntimeError(f"No .SAFE root found inside {zip_path.name}")

    existing_roots = [out_dir / root for root in safe_roots if (out_dir / root).exists()]
    if existing_roots and not force:
        print(f"Skip existing: {zip_path.name}")
        for root in existing_roots:
            print(f"  {root.name}")
        return "skipped", uncompressed_size

    temp_dir = out_dir / f".extracting_{zip_path.stem}"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    print(f"Extracting: {zip_path.name}")
    print(f"  expected uncompressed: {human_size(uncompressed_size)}")

    try:
        subprocess.run(
            ["unzip", "-q", str(zip_path), "-d", str(temp_dir)],
            check=True,
        )

        for root in safe_roots:
            extracted_root = temp_dir / root
            final_root = out_dir / root

            if final_root.exists():
                if force:
                    shutil.rmtree(final_root)
                else:
                    continue

            shutil.move(str(extracted_root), str(final_root))

        shutil.rmtree(temp_dir)
        print(f"Done: {zip_path.name}")
        return "extracted", uncompressed_size
    except Exception:
        print(f"Failed: {zip_path.name}")
        print(f"  partial extraction kept at: {temp_dir}")
        raise


def main():
    parser = argparse.ArgumentParser(description="Extract Sentinel-1 zip files to .SAFE folders.")
    parser.add_argument(
        "directory",
        nargs="?",
        default="sentinel1_jeju",
        help="Directory containing Sentinel-1 zip files.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only summarize what would be extracted.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing .SAFE folders.",
    )
    args = parser.parse_args()

    out_dir = Path(args.directory).expanduser().resolve()
    zip_paths = sorted(out_dir.glob("*.zip"))

    if not zip_paths:
        raise SystemExit(f"No zip files found in {out_dir}")

    pending = []
    skipped = []
    total_uncompressed = 0

    for zip_path in zip_paths:
        roots, uncompressed_size = zip_info(zip_path)
        total_uncompressed += uncompressed_size
        safe_roots = [root for root in roots if root.endswith(".SAFE")]
        existing = safe_roots and all((out_dir / root).exists() for root in safe_roots)

        if existing and not args.force:
            skipped.append(zip_path)
        else:
            pending.append(zip_path)

    print(f"Zip files: {len(zip_paths)}")
    print(f"Already extracted: {len(skipped)}")
    print(f"Pending extraction: {len(pending)}")
    print(f"Total uncompressed size estimate: {human_size(total_uncompressed)}")

    if args.dry_run:
        return

    extracted_count = 0
    skipped_count = 0

    for zip_path in pending:
        status, _ = extract_zip(zip_path, out_dir, force=args.force)
        if status == "extracted":
            extracted_count += 1
        elif status == "skipped":
            skipped_count += 1

    print("")
    print("Extraction complete")
    print(f"Extracted: {extracted_count}")
    print(f"Skipped: {len(skipped) + skipped_count}")


if __name__ == "__main__":
    main()
