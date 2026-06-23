import csv
import json
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET


S1_NAME_RE = re.compile(
    r"^(?P<mission>S1[A-Z])_"
    r"(?P<mode>[A-Z]{2})_"
    r"(?P<product>SLC)__"
    r"(?P<level>\d)"
    r"(?P<class>[A-Z])"
    r"(?P<polarization>[A-Z]{2})_"
    r"(?P<start>\d{8}T\d{6})_"
    r"(?P<stop>\d{8}T\d{6})_"
    r"(?P<orbit>\d{6})_"
    r"(?P<datatake>[0-9A-F]{6})_"
    r"(?P<unique>[0-9A-F]{4})"
)


NS = {
    "safe": "http://www.esa.int/safe/sentinel-1.0",
    "s1": "http://www.esa.int/safe/sentinel-1.0/sentinel-1",
    "gml": "http://www.opengis.net/gml",
}


def human_size(size):
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def parse_datetime(value):
    return datetime.strptime(value, "%Y%m%dT%H%M%S")


def text_or_none(root, path):
    node = root.find(path, NS)
    if node is not None and node.text:
        return node.text.strip()
    return None


def plain_text_or_none(root, path):
    node = root.find(path)
    if node is not None and node.text:
        return node.text.strip()
    return None


def attr_text_or_none(root, path, attr_name, attr_value):
    for node in root.findall(path, NS):
        if node.attrib.get(attr_name) == attr_value and node.text:
            return node.text.strip()
    return None


def parse_coordinates(value):
    if not value:
        return []
    points = []
    for pair in value.split():
        lat, lon = pair.split(",", 1)
        points.append([float(lat), float(lon)])
    if points and points[0] != points[-1]:
        points.append(points[0])
    return points


def polygon_bounds(points):
    if not points:
        return None
    lats = [point[0] for point in points]
    lons = [point[1] for point in points]
    return {
        "south": min(lats),
        "west": min(lons),
        "north": max(lats),
        "east": max(lons),
    }


def polygon_center(points):
    bounds = polygon_bounds(points)
    if not bounds:
        return None
    return [
        (bounds["south"] + bounds["north"]) / 2,
        (bounds["west"] + bounds["east"]) / 2,
    ]


def child_float(node, tag):
    child = node.find(tag) if node is not None else None
    if child is None or not child.text:
        return None
    return float(child.text.strip())


def geog_node_point(root, tag):
    node = root.find(f".//{tag}")
    lat = child_float(node, "Latitude")
    lon = child_float(node, "Longitude")
    if lat is None or lon is None:
        return None
    return [lat, lon]


def geodetic_text_point(root, tag):
    text = plain_text_or_none(root, f".//{tag}")
    if not text:
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) < 2:
        return None
    return [float(parts[0]), float(parts[1])]


def first_existing(path, patterns):
    for pattern in patterns:
        found = sorted(path.glob(pattern))
        if found:
            return found[0]
    return None


def file_size(path):
    return path.stat().st_size if path and path.exists() else None


def parse_loose_datetime(value):
    if not value:
        return None
    text = value.strip().replace(" ", "T")
    if "." in text:
        head, tail = text.split(".", 1)
        digits = "".join(char for char in tail if char.isdigit())
        suffix = tail[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{suffix}"
    for fmt in ("%Y%m%d%H%M%S.%f", "%Y%m%d%H%M%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def scene_status(has_metadata, has_preview):
    if not has_metadata:
        return "metadata-missing"
    if not has_preview:
        return "preview-missing"
    return "ready"


def make_directory_scene(
    *,
    product_dir,
    collection,
    provider,
    sensor_family,
    mission,
    satellite,
    instrument_mode,
    product_type,
    level,
    polarization,
    start_dt,
    stop_dt,
    orbit,
    pass_direction,
    footprint,
    metadata_ok,
    quicklook_path,
    thumbnail_path,
    archive_path=None,
    extra_metadata=None,
):
    safe_size = directory_size(product_dir)
    zip_size = file_size(archive_path)
    preview_exists = bool(quicklook_path and quicklook_path.exists())
    return {
        "id": product_dir.name,
        "name": product_dir.name,
        "collection": collection,
        "provider": provider,
        "sensor_family": sensor_family,
        "mission": mission,
        "satellite": satellite,
        "instrument_mode": instrument_mode or "",
        "product_type": product_type or "",
        "level": level or "",
        "polarization": polarization or "",
        "start": start_dt.isoformat() if start_dt else "",
        "stop": stop_dt.isoformat() if stop_dt else "",
        "date": start_dt.date().isoformat() if start_dt else "",
        "year_month": start_dt.strftime("%Y-%m") if start_dt else "",
        "orbit": orbit or "",
        "relative_orbit": "",
        "pass_direction": pass_direction or "UNKNOWN",
        "datatake": "",
        "unique": "",
        "zip_path": str(archive_path) if archive_path else "",
        "zip_exists": bool(archive_path and archive_path.exists()),
        "zip_status": zip_status(archive_path) if archive_path else "missing",
        "zip_size": zip_size,
        "zip_size_label": human_size(zip_size),
        "safe_path": str(product_dir),
        "safe_exists": True,
        "safe_size": safe_size,
        "safe_size_label": human_size(safe_size),
        "manifest_ok": metadata_ok,
        "processing_status": scene_status(metadata_ok, preview_exists),
        "processing_start": "",
        "footprint": footprint,
        "bounds": polygon_bounds(footprint),
        "center": polygon_center(footprint),
        "thumbnail_kind": "image",
        "metadata": {
            **(extra_metadata or {}),
            "source_format": "directory",
            "quicklook_path": str(quicklook_path) if quicklook_path else "",
            "thumbnail_path": str(thumbnail_path) if thumbnail_path else "",
        },
    }


def zip_status(path):
    if not path.exists():
        return "missing"
    if path.stat().st_size <= 0:
        return "empty"
    return "ok"


def directory_size(path):
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return total


def parse_sentinel1_safe(safe_dir, zip_dir):
    name = safe_dir.name.removesuffix(".SAFE")
    match = S1_NAME_RE.match(name)
    if not match:
        return None

    data = match.groupdict()
    start_dt = parse_datetime(data["start"])
    stop_dt = parse_datetime(data["stop"])
    manifest = safe_dir / "manifest.safe"
    manifest_ok = manifest.exists()
    pass_direction = None
    relative_orbit = None
    footprint = []
    processing_start = None

    if manifest_ok:
        root = ET.parse(manifest).getroot()
        pass_direction = text_or_none(root, ".//s1:pass")
        relative_orbit = attr_text_or_none(
            root,
            ".//safe:relativeOrbitNumber",
            "type",
            "start",
        )
        processing_node = root.find(".//safe:processing", NS)
        if processing_node is not None:
            processing_start = processing_node.attrib.get("start")
        coordinate_text = text_or_none(root, ".//safe:footPrint/gml:coordinates")
        footprint = parse_coordinates(coordinate_text)

    zip_path = zip_dir / f"{name}.zip"
    zip_size = zip_path.stat().st_size if zip_path.exists() else None
    # SAFE folders contain many large measurement files. Prefer the matching zip
    # size for fast catalog scans; fall back to a full walk only when the zip is absent.
    safe_size = zip_size if zip_size is not None else directory_size(safe_dir)

    processing_status = "ready"
    if not manifest_ok:
        processing_status = "metadata-missing"
    elif zip_status(zip_path) != "ok":
        processing_status = "zip-issue"

    return {
        "id": name,
        "name": name,
        "collection": "Sentinel-1",
        "provider": "Copernicus",
        "sensor_family": "SAR",
        "mission": data["mission"],
        "satellite": data["mission"],
        "instrument_mode": data["mode"],
        "product_type": data["product"],
        "level": data["level"],
        "polarization": data["polarization"],
        "start": start_dt.isoformat(),
        "stop": stop_dt.isoformat(),
        "date": start_dt.date().isoformat(),
        "year_month": start_dt.strftime("%Y-%m"),
        "orbit": data["orbit"],
        "relative_orbit": relative_orbit,
        "pass_direction": pass_direction,
        "datatake": data["datatake"],
        "unique": data["unique"],
        "zip_path": str(zip_path),
        "zip_exists": zip_path.exists(),
        "zip_status": zip_status(zip_path),
        "zip_size": zip_size,
        "zip_size_label": human_size(zip_size),
        "safe_path": str(safe_dir),
        "safe_exists": True,
        "safe_size": safe_size,
        "safe_size_label": human_size(safe_size),
        "manifest_ok": manifest_ok,
        "processing_status": processing_status,
        "processing_start": processing_start,
        "footprint": footprint,
        "bounds": polygon_bounds(footprint),
        "center": polygon_center(footprint),
        "thumbnail_kind": "footprint",
        "metadata": {
            "adapter": "sentinel1_safe",
            "source_format": "SAFE",
        },
    }


def parse_kompsat3(product_dir):
    aux_path = first_existing(product_dir, ["*_Aux.xml"])
    if not aux_path:
        return None

    root = ET.parse(aux_path).getroot()
    name = product_dir.name
    quicklook_path = first_existing(product_dir, ["*_br.jpg", "*_BR.jpg"])
    thumbnail_path = first_existing(product_dir, ["*_th.jpg", "*_TH.jpg"])
    start_dt = parse_loose_datetime(plain_text_or_none(root, ".//ImagingCenterTime/UTC"))
    if not start_dt:
        start_dt = parse_loose_datetime(plain_text_or_none(root, ".//CreateDate"))
    if not start_dt:
        match = re.search(r"K3_(\d{14})", name)
        start_dt = parse_loose_datetime(match.group(1)) if match else None

    footprint = [
        geog_node_point(root, "ImageGeogTL"),
        geog_node_point(root, "ImageGeogTR"),
        geog_node_point(root, "ImageGeogBR"),
        geog_node_point(root, "ImageGeogBL"),
    ]
    footprint = [point for point in footprint if point]
    if footprint and footprint[0] != footprint[-1]:
        footprint.append(footprint[0])

    orbit_direction = plain_text_or_none(root, ".//General/OrbitDirection") or ""
    pass_direction = orbit_direction.split()[0].upper() if orbit_direction else "UNKNOWN"

    return make_directory_scene(
        product_dir=product_dir,
        collection="Kompsat3",
        provider="KARI",
        sensor_family="Optical",
        mission="KOMPSAT-3",
        satellite=plain_text_or_none(root, ".//General/Satellite") or "KOMPSAT-3",
        instrument_mode=plain_text_or_none(root, ".//General/ImagingMode"),
        product_type="Optical",
        level=plain_text_or_none(root, ".//General/ProductLevel"),
        polarization="",
        start_dt=start_dt,
        stop_dt=start_dt,
        orbit=plain_text_or_none(root, ".//General/OrbitNumber"),
        pass_direction=pass_direction,
        footprint=footprint,
        metadata_ok=True,
        quicklook_path=quicklook_path,
        thumbnail_path=thumbnail_path,
        extra_metadata={
            "adapter": "kompsat3_directory",
            "aux_path": str(aux_path),
            "sensor": plain_text_or_none(root, ".//General/Sensor") or "",
        },
    )


def parse_kompsat5(product_dir):
    aux_path = first_existing(product_dir, ["*_Aux.xml"])
    if not aux_path:
        return None

    root = ET.parse(aux_path).getroot()
    quicklook_path = first_existing(product_dir, ["*_QL.png", "*_br.jpg", "*_BR.jpg"])
    thumbnail_path = first_existing(product_dir, ["*_th.jpg", "*_TH.jpg", "*_QL.png"])
    start_dt = parse_loose_datetime(plain_text_or_none(root, ".//SceneSensingStartUTC"))
    stop_dt = parse_loose_datetime(plain_text_or_none(root, ".//SceneSensingStopUTC")) or start_dt

    footprint = [
        geodetic_text_point(root, "TopLeftGeodeticCoordinates"),
        geodetic_text_point(root, "TopRightGeodeticCoordinates"),
        geodetic_text_point(root, "BottomRightGeodeticCoordinates"),
        geodetic_text_point(root, "BottomLeftGeodeticCoordinates"),
    ]
    footprint = [point for point in footprint if point]
    if footprint and footprint[0] != footprint[-1]:
        footprint.append(footprint[0])

    product_type = plain_text_or_none(root, ".//ProductType") or ""
    beam = plain_text_or_none(root, ".//MultiBeamID") or ""
    acquisition_mode = plain_text_or_none(root, ".//AcquisitionMode") or ""
    polarization = plain_text_or_none(root, ".//SubSwaths/SubSwath/Polarisation") or ""

    return make_directory_scene(
        product_dir=product_dir,
        collection="Kompsat5",
        provider="KARI",
        sensor_family="SAR",
        mission="KOMPSAT-5",
        satellite=plain_text_or_none(root, ".//SatelliteID") or "KOMPSAT-5",
        instrument_mode=beam or acquisition_mode,
        product_type=product_type,
        level="L1A",
        polarization=polarization,
        start_dt=start_dt,
        stop_dt=stop_dt,
        orbit=plain_text_or_none(root, ".//OrbitNumber"),
        pass_direction=plain_text_or_none(root, ".//OrbitDirection") or "UNKNOWN",
        footprint=footprint,
        metadata_ok=True,
        quicklook_path=quicklook_path,
        thumbnail_path=thumbnail_path,
        extra_metadata={
            "adapter": "kompsat5_directory",
            "aux_path": str(aux_path),
            "acquisition_mode": acquisition_mode,
            "look_side": plain_text_or_none(root, ".//LookSide") or "",
        },
    )


def scan_catalog(data_dir):
    data_path = Path(data_dir).expanduser().resolve()
    scenes = []

    sentinel_root = data_path / "Sentinel-1" if (data_path / "Sentinel-1").exists() else data_path
    for safe_dir in sorted(sentinel_root.glob("*.SAFE")):
        scene = parse_sentinel1_safe(safe_dir, sentinel_root)
        if scene:
            scenes.append(scene)

    kompsat3_root = data_path / "Kompsat3"
    if kompsat3_root.exists():
        for product_dir in sorted(path for path in kompsat3_root.iterdir() if path.is_dir()):
            scene = parse_kompsat3(product_dir)
            if scene:
                scenes.append(scene)

    kompsat5_root = data_path / "Kompsat5"
    if kompsat5_root.exists():
        for product_dir in sorted(path for path in kompsat5_root.iterdir() if path.is_dir()):
            scene = parse_kompsat5(product_dir)
            if scene:
                scenes.append(scene)

    scenes.sort(key=lambda item: item["start"], reverse=True)
    return scenes


def summarize(scenes):
    mission_counts = Counter(scene["mission"] for scene in scenes)
    direction_counts = Counter(scene["pass_direction"] or "UNKNOWN" for scene in scenes)
    status_counts = Counter(scene["processing_status"] for scene in scenes)
    month_counts = Counter(scene["year_month"] for scene in scenes)
    dates = sorted({scene["date"] for scene in scenes})
    duplicate_dates = {
        date: count for date, count in Counter(scene["date"] for scene in scenes).items()
        if count > 1
    }
    gaps = []
    for previous, current in zip(dates, dates[1:]):
        prev_dt = datetime.strptime(previous, "%Y-%m-%d")
        cur_dt = datetime.strptime(current, "%Y-%m-%d")
        days = (cur_dt - prev_dt).days
        if days > 12:
            gaps.append({"from": previous, "to": current, "days": days})

    bounds = None
    all_bounds = [scene["bounds"] for scene in scenes if scene.get("bounds")]
    if all_bounds:
        bounds = {
            "south": min(item["south"] for item in all_bounds),
            "west": min(item["west"] for item in all_bounds),
            "north": max(item["north"] for item in all_bounds),
            "east": max(item["east"] for item in all_bounds),
        }

    return {
        "scene_count": len(scenes),
        "collections": sorted({scene["collection"] for scene in scenes}),
        "sensor_families": sorted({scene["sensor_family"] for scene in scenes}),
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "mission_counts": dict(sorted(mission_counts.items())),
        "direction_counts": dict(sorted(direction_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
        "month_counts": dict(sorted(month_counts.items())),
        "duplicate_dates": dict(sorted(duplicate_dates.items())),
        "date_gaps_over_12_days": gaps,
        "bounds": bounds,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_inventory_csv(scenes, csv_path):
    fields = [
        "id",
        "collection",
        "mission",
        "sensor_family",
        "product_type",
        "instrument_mode",
        "polarization",
        "date",
        "start",
        "pass_direction",
        "relative_orbit",
        "orbit",
        "zip_status",
        "zip_size_label",
        "safe_size_label",
        "processing_status",
        "safe_path",
    ]
    with Path(csv_path).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for scene in scenes:
            writer.writerow({field: scene.get(field, "") for field in fields})


def write_catalog_json(scenes, summary, output_path):
    Path(output_path).write_text(
        json.dumps({"summary": summary, "scenes": scenes}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
