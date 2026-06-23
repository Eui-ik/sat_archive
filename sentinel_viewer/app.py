#!/usr/bin/env python3
import base64
from http import cookies
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import shutil
import secrets
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from catalog import directory_size, scan_catalog, summarize


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DEFAULT_DATA_DIR = ROOT.parent / "sentinel1_jeju"
DEFAULT_DB_PATH = Path(os.environ.get("SAR_VIEWER_DB", ROOT.parent / "viewer_state" / "sar_viewer.sqlite3"))
ADMIN_EMAIL = os.environ.get("SAR_VIEWER_ADMIN_EMAIL", "euiik@innopam.com").strip().lower()
SESSION_COOKIE = "sar_viewer_session"
SESSION_TTL_SECONDS = int(os.environ.get("SAR_VIEWER_SESSION_TTL_SECONDS", str(7 * 24 * 60 * 60)))
TRASH_RETENTION_DAYS = int(os.environ.get("SAR_VIEWER_TRASH_RETENTION_DAYS", "30"))
AUTO_RESCAN_INTERVAL_SECONDS = int(os.environ.get("SAR_VIEWER_AUTO_RESCAN_INTERVAL_SECONDS", "600"))
AUTO_RESCAN_ENABLED = os.environ.get("SAR_VIEWER_AUTO_RESCAN_ENABLED", "1") != "0"
MAX_JSON_BODY_BYTES = int(os.environ.get("SAR_VIEWER_MAX_JSON_BODY_BYTES", str(64 * 1024)))
MAX_SEARCH_TOP = int(os.environ.get("SAR_VIEWER_MAX_SEARCH_TOP", "200"))
MAX_DAYS_BACK = int(os.environ.get("SAR_VIEWER_MAX_DAYS_BACK", str(365 * 10)))
MAX_ON_DEMAND_ZIP_BYTES = int(os.environ.get("SAR_VIEWER_MAX_ON_DEMAND_ZIP_BYTES", str(20 * 1024 * 1024 * 1024)))
ZIP_SEMAPHORE = threading.Semaphore(int(os.environ.get("SAR_VIEWER_MAX_ZIP_JOBS", "1")))
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)
JEJU_WKT = (
    "POLYGON(("
    "126.05 33.05,"
    "127.05 33.05,"
    "127.05 33.75,"
    "126.05 33.75,"
    "126.05 33.05"
    "))"
)


def bbox_to_wkt(bbox):
    try:
        west, south, east, north = [float(value) for value in bbox]
    except (TypeError, ValueError):
        raise ValueError("bbox must contain west,south,east,north numeric values.")
    if not (-180 <= west <= 180 and -180 <= east <= 180 and -90 <= south <= 90 and -90 <= north <= 90):
        raise ValueError("bbox coordinates are outside valid longitude/latitude ranges.")
    if west >= east or south >= north:
        raise ValueError("bbox must satisfy west < east and south < north.")
    return (
        "POLYGON(("
        f"{west} {south},"
        f"{east} {south},"
        f"{east} {north},"
        f"{west} {north},"
        f"{west} {south}"
        "))"
    )


def parse_bbox(value):
    if not value:
        return None
    if isinstance(value, str):
        parts = value.split(",")
    else:
        parts = value
    if len(parts) != 4:
        raise ValueError("bbox must have 4 values: west,south,east,north.")
    return [float(part) for part in parts]


def bounded_int(value, default, minimum, maximum, field_name):
    try:
        number = int(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a number.")
    if number < minimum:
        return minimum
    if number > maximum:
        return maximum
    return number


def is_relative_to(path, root):
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def ensure_data_path(path, data_root):
    resolved = Path(path).resolve()
    if not is_relative_to(resolved, data_root):
        raise RuntimeError("Data path is outside the configured data directory.")
    return resolved


def safe_download_name(name):
    cleaned = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    return cleaned or "download.bin"


def safe_path_part(name):
    return safe_download_name(name).strip(".") or secrets.token_hex(8)


def move_to_directory(path, target_dir):
    if not path or not path.exists():
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if target.exists():
        target = target_dir / f"{path.stem}-{secrets.token_hex(4)}{path.suffix}"
    shutil.move(str(path), str(target))
    return target


def restore_from_directory(source_dir, original_path):
    target = Path(original_path)
    source = source_dir / target.name
    if not source.exists():
        return False
    if target.exists():
        raise RuntimeError(f"Restore target already exists: {target.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return True


def parse_year_month(value, field_name):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"{field_name} must be formatted as YYYY-MM.")


def next_month(dt):
    if dt.month == 12:
        return dt.replace(year=dt.year + 1, month=1)
    return dt.replace(month=dt.month + 1)


def resolve_search_period(days_back=None, month_from=None, month_to=None):
    start_dt = parse_year_month(month_from, "from")
    end_month_dt = parse_year_month(month_to, "to")
    if start_dt or end_month_dt:
        if not start_dt or not end_month_dt:
            raise ValueError("Both from and to months are required.")
        end_dt = next_month(end_month_dt)
        if start_dt >= end_dt:
            raise ValueError("from month must be before or equal to to month.")
        return start_dt, end_dt

    end_dt = datetime.now(timezone.utc)
    days = int(days_back if days_back is not None else 30)
    start_dt = datetime.fromtimestamp(end_dt.timestamp() - (days * 86400), timezone.utc)
    return start_dt, end_dt


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class ExclusionStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS excluded_scenes (
                    scene_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    reason TEXT DEFAULT '',
                    excluded_at TEXT NOT NULL,
                    safe_path TEXT DEFAULT '',
                    zip_path TEXT DEFAULT '',
                    deleted_safe INTEGER DEFAULT 0,
                    deleted_zip INTEGER DEFAULT 0,
                    safe_size INTEGER DEFAULT 0,
                    zip_size INTEGER DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'excluded',
                    delete_after TEXT DEFAULT '',
                    trash_path TEXT DEFAULT '',
                    excluded_by TEXT DEFAULT ''
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(excluded_scenes)").fetchall()
            }
            migrations = {
                "status": "ALTER TABLE excluded_scenes ADD COLUMN status TEXT NOT NULL DEFAULT 'excluded'",
                "delete_after": "ALTER TABLE excluded_scenes ADD COLUMN delete_after TEXT DEFAULT ''",
                "trash_path": "ALTER TABLE excluded_scenes ADD COLUMN trash_path TEXT DEFAULT ''",
                "excluded_by": "ALTER TABLE excluded_scenes ADD COLUMN excluded_by TEXT DEFAULT ''",
            }
            for column, sql in migrations.items():
                if column not in columns:
                    conn.execute(sql)

    def ids(self):
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT scene_id FROM excluded_scenes WHERE status IN ('excluded', 'trash', 'purged')"
            ).fetchall()
        return {row[0] for row in rows}

    def count(self):
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM excluded_scenes").fetchone()
        return row[0]

    def trash_count(self):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM excluded_scenes WHERE status = 'trash'"
            ).fetchone()
        return row[0]

    def contains(self, scene_id):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM excluded_scenes WHERE scene_id = ?",
                (scene_id,),
            ).fetchone()
        return row is not None

    def list(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM excluded_scenes ORDER BY excluded_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_trash(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT *
                FROM excluded_scenes
                WHERE status = 'trash'
                ORDER BY excluded_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_trash(self, scene_id):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT *
                FROM excluded_scenes
                WHERE scene_id = ? AND status = 'trash'
                """,
                (scene_id,),
            ).fetchone()
        return dict(row) if row else None

    def add_trash(self, scene, reason, trash_path, delete_after, excluded_by):
        excluded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO excluded_scenes (
                    scene_id, name, reason, excluded_at, safe_path, zip_path,
                    deleted_safe, deleted_zip, safe_size, zip_size,
                    status, delete_after, trash_path, excluded_by
                )
                VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'trash', ?, ?, ?)
                ON CONFLICT(scene_id) DO UPDATE SET
                    name = excluded.name,
                    reason = excluded.reason,
                    excluded_at = excluded.excluded_at,
                    safe_path = excluded.safe_path,
                    zip_path = excluded.zip_path,
                    deleted_safe = excluded.deleted_safe,
                    deleted_zip = excluded.deleted_zip,
                    safe_size = excluded.safe_size,
                    zip_size = excluded.zip_size,
                    status = excluded.status,
                    delete_after = excluded.delete_after,
                    trash_path = excluded.trash_path,
                    excluded_by = excluded.excluded_by
                """,
                (
                    scene["id"],
                    scene["name"],
                    reason,
                    excluded_at,
                    scene["path"],
                    scene["zip_path"],
                    int(scene.get("safe_size") or 0),
                    int(scene.get("zip_size") or 0),
                    delete_after,
                    trash_path,
                    excluded_by,
                ),
            )

    def mark_purged(self, scene_id, deleted_safe=True, deleted_zip=True):
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE excluded_scenes
                SET status = 'purged',
                    deleted_safe = ?,
                    deleted_zip = ?,
                    trash_path = ''
                WHERE scene_id = ?
                """,
                (int(deleted_safe), int(deleted_zip), scene_id),
            )

    def remove(self, scene_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM excluded_scenes WHERE scene_id = ?", (scene_id,))

    def due_trash(self, now_iso):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT scene_id, trash_path
                FROM excluded_scenes
                WHERE status = 'trash'
                  AND delete_after != ''
                  AND trash_path != ''
                  AND delete_after <= ?
                """,
                (now_iso,),
            ).fetchall()
        return [dict(row) for row in rows]


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password, salt=None):
    salt_bytes = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_bytes, 210000)
    return (
        base64.urlsafe_b64encode(salt_bytes).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password, salt, password_hash):
    salt_bytes = base64.urlsafe_b64decode(salt.encode("ascii"))
    _, candidate = hash_password(password, salt=salt_bytes)
    return hmac.compare_digest(candidate, password_hash)


def session_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, db_path, admin_email):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.admin_email = admin_email.lower()
        self._init_db()
        self.ensure_admin()

    def connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    email TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "active" not in columns:
                conn.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(email) REFERENCES users(email)
                )
                """
            )

    def ensure_admin(self):
        configured_password = os.environ.get("SAR_VIEWER_ADMIN_PASSWORD", "")
        password = configured_password or secrets.token_urlsafe(18)
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT email, salt, password_hash FROM users WHERE email = ?",
                (self.admin_email,),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE users
                    SET role = 'admin', active = 1, updated_at = ?
                    WHERE email = ?
                    """,
                    (now, self.admin_email),
                )
                if configured_password and not verify_password(configured_password, row[1], row[2]):
                    salt, password_hash = hash_password(configured_password)
                    conn.execute(
                        """
                        UPDATE users
                        SET role = 'admin', active = 1, salt = ?, password_hash = ?, updated_at = ?
                        WHERE email = ?
                        """,
                        (salt, password_hash, now, self.admin_email),
                    )
                    print(f"Admin password updated from SAR_VIEWER_ADMIN_PASSWORD: {self.admin_email}", flush=True)
                return
            salt, password_hash = hash_password(password)
            conn.execute(
                """
                INSERT INTO users (email, role, salt, password_hash, created_at, updated_at)
                VALUES (?, 'admin', ?, ?, ?, ?)
                """,
                (self.admin_email, salt, password_hash, now, now),
            )
        if configured_password:
            print(f"Admin user ready: {self.admin_email}", flush=True)
        else:
            print(
                "Generated temporary admin password. "
                f"email={self.admin_email} password={password}",
                flush=True,
            )

    def authenticate(self, email, password):
        normalized = email.strip().lower()
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT email, role, salt, password_hash, active FROM users WHERE email = ?",
                (normalized,),
            ).fetchone()
        if (
            not row
            or not row["active"]
            or not verify_password(password, row["salt"], row["password_hash"])
        ):
            return None
        return {"email": row["email"], "role": row["role"]}

    def create_session(self, email):
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (token_hash, email, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (session_hash(token), email, utc_now_iso(), expires_at),
            )
        return token, expires_at

    def user_for_token(self, token):
        if not token:
            return None
        now = int(time.time())
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            row = conn.execute(
                """
                SELECT users.email, users.role
                FROM sessions
                JOIN users ON users.email = sessions.email
                WHERE sessions.token_hash = ?
                  AND sessions.expires_at >= ?
                  AND users.active = 1
                """,
                (session_hash(token), now),
            ).fetchone()
        if not row:
            return None
        return {"email": row["email"], "role": row["role"]}

    def delete_session(self, token):
        if not token:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session_hash(token),))

    def list_users(self):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT email, role, active, created_at, updated_at
                FROM users
                ORDER BY role = 'admin' DESC, email
                """
            ).fetchall()
        return [self.public_user(row) for row in rows]

    def public_user(self, row):
        return {
            "email": row["email"],
            "role": row["role"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_user(self, email):
        with self.connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                """
                SELECT email, role, active, created_at, updated_at
                FROM users
                WHERE email = ?
                """,
                (email.strip().lower(),),
            ).fetchone()

    def active_admin_count(self, exclude_email=None):
        query = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
        params = []
        if exclude_email:
            query += " AND email != ?"
            params.append(exclude_email)
        with self.connect() as conn:
            return conn.execute(query, params).fetchone()[0]

    def create_user(self, email, password, role="user"):
        normalized = email.strip().lower()
        if not normalized or "@" not in normalized:
            raise ValueError("올바른 이메일 주소를 입력해 주세요.")
        if len(password) < 8:
            raise ValueError("비밀번호는 8자 이상이어야 합니다.")
        if role not in {"admin", "user"}:
            raise ValueError("지원하지 않는 권한입니다.")
        now = utc_now_iso()
        salt, password_hash = hash_password(password)
        with self.connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users (email, role, active, salt, password_hash, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    """,
                    (normalized, role, salt, password_hash, now, now),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("이미 등록된 사용자입니다.") from error
        return self.public_user(self.get_user(normalized))

    def update_user(self, email, role=None, active=None, password=None, actor_email=""):
        normalized = email.strip().lower()
        row = self.get_user(normalized)
        if not row:
            raise KeyError("사용자를 찾을 수 없습니다.")
        next_role = row["role"] if role in (None, "") else role
        next_active = bool(row["active"]) if active is None else bool(active)
        if next_role not in {"admin", "user"}:
            raise ValueError("지원하지 않는 권한입니다.")
        if normalized == actor_email and not next_active:
            raise ValueError("본인 계정은 비활성화할 수 없습니다.")
        losing_admin = row["role"] == "admin" and (next_role != "admin" or not next_active)
        if losing_admin and self.active_admin_count(exclude_email=normalized) < 1:
            raise ValueError("활성 관리자 계정은 최소 1개 이상 필요합니다.")

        fields = ["role = ?", "active = ?", "updated_at = ?"]
        params = [next_role, 1 if next_active else 0, utc_now_iso()]
        if password:
            if len(password) < 8:
                raise ValueError("비밀번호는 8자 이상이어야 합니다.")
            salt, password_hash = hash_password(password)
            fields.extend(["salt = ?", "password_hash = ?"])
            params.extend([salt, password_hash])
        params.append(normalized)

        with self.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE email = ?", params)
            if not next_active:
                conn.execute("DELETE FROM sessions WHERE email = ?", (normalized,))
        return self.public_user(self.get_user(normalized))


def to_common_scene(scene):
    footprint = scene.get("footprint") or []
    geo_coords = [[point[1], point[0]] for point in footprint]
    bounds = scene.get("bounds")
    bbox = None
    if bounds:
        bbox = [bounds["west"], bounds["south"], bounds["east"], bounds["north"]]

    metadata = scene.get("metadata") or {}
    preview_quicklook = Path(metadata["quicklook_path"]) if metadata.get("quicklook_path") else Path(scene["safe_path"]) / "preview" / "quick-look.png"
    preview_thumbnail = Path(metadata["thumbnail_path"]) if metadata.get("thumbnail_path") else Path(scene["safe_path"]) / "preview" / "thumbnail.png"

    return {
        "id": scene["id"],
        "name": scene["name"] if str(scene["name"]).endswith(".SAFE") else scene["name"] + (".SAFE" if metadata.get("adapter") == "sentinel1_safe" else ""),
        "satellite_family": scene["collection"],
        "mission": scene["mission"],
        "sensor": scene["sensor_family"],
        "product_type": scene["product_type"],
        "level": scene["level"],
        "mode": scene["instrument_mode"],
        "polarization": scene["polarization"],
        "start_time": scene["start"],
        "stop_time": scene["stop"],
        "date": scene["date"],
        "absolute_orbit": scene["orbit"],
        "relative_orbit": scene["relative_orbit"] or "",
        "orbit_direction": scene["pass_direction"] or "UNKNOWN",
        "datatake": scene["datatake"],
        "unique_id": scene["unique"],
        "path": scene["safe_path"],
        "zip_path": scene["zip_path"] if scene["zip_exists"] else "",
        "safe_size": scene["safe_size"],
        "safe_size_label": scene["safe_size_label"],
        "zip_size": scene["zip_size"],
        "zip_size_label": scene["zip_size_label"],
        "status": {
            "zip": scene["zip_exists"],
            "safe": scene["safe_exists"],
            "manifest": scene["manifest_ok"],
            "preview": preview_quicklook.exists(),
            "processing": scene["processing_status"],
        },
        "preview": {
            "quicklook_url": f"/api/preview/{scene['id']}/quick-look.png" if preview_quicklook.exists() else "",
            "thumbnail_url": f"/api/preview/{scene['id']}/thumbnail.png" if preview_thumbnail.exists() else "",
            "quicklook_path": str(preview_quicklook) if preview_quicklook.exists() else "",
            "thumbnail_path": str(preview_thumbnail) if preview_thumbnail.exists() else "",
        },
        "footprint": {
            "type": "Polygon",
            "coordinates": [geo_coords],
        } if geo_coords else None,
        "bbox": bbox,
        "center": scene["center"],
        "metadata": scene["metadata"],
    }


def human_size(size):
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size or 0)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024


def product_to_scene_id(product_name):
    return product_name.replace(".SAFE", "")


def product_to_zip_name(product_name):
    return f"{product_to_scene_id(product_name)}.zip"


def parse_content_start(content_date):
    if isinstance(content_date, dict):
        return content_date.get("Start", "")
    return ""


def http_json(url, params=None, data=None, headers=None, timeout=120):
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    body = None
    request_headers = dict(headers or {})
    if data is not None:
        body = urlencode(data).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    request = Request(full_url, data=body, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_extract_zip(archive, target_dir):
    target_root = target_dir.resolve()
    for member in archive.infolist():
        member_path = (target_dir / member.filename).resolve()
        if member_path != target_root and target_root not in member_path.parents:
            raise RuntimeError(f"Unsafe zip member path: {member.filename}")
    archive.extractall(target_dir)


def search_cdse_sentinel1(days_back=None, top=20, product_type="IW_SLC__1S", bbox=None, month_from=None, month_to=None):
    start_dt, end_dt = resolve_search_period(days_back=days_back, month_from=month_from, month_to=month_to)
    start_str = start_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str = end_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    area_wkt = bbox_to_wkt(bbox) if bbox else JEJU_WKT

    odata_filter = (
        "Collection/Name eq 'SENTINEL-1' "
        f"and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' "
        f"and att/OData.CSC.StringAttribute/Value eq '{product_type}') "
        f"and ContentDate/Start ge {start_str} "
        f"and ContentDate/Start lt {end_str} "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{area_wkt}')"
    )

    payload = http_json(
        CATALOGUE_URL,
        params={
            "$filter": odata_filter,
            "$orderby": "ContentDate/Start desc",
            "$top": str(top),
            "$select": "Id,Name,ContentDate,PublicationDate,Online,ContentLength,S3Path,GeoFootprint",
        },
    )
    return payload.get("value", [])


def get_cdse_token():
    username = os.environ.get("CDSE_USERNAME")
    password = os.environ.get("CDSE_PASSWORD")
    if not username or not password:
        raise RuntimeError("CDSE_USERNAME/CDSE_PASSWORD environment variables are not configured.")
    payload = http_json(
        TOKEN_URL,
        data={
            "client_id": "cdse-public",
            "username": username,
            "password": password,
            "grant_type": "password",
        },
        timeout=60,
    )
    return payload["access_token"]


class CatalogStore:
    def __init__(self, data_dir, exclusions):
        self.data_dir = Path(data_dir).resolve()
        self.trash_dir = self.data_dir / ".trash"
        self.exclusions = exclusions
        self.lock = threading.RLock()
        self.scenes = []
        self.summary = {}
        self.rescan()

    def purge_expired_trash(self):
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for item in self.exclusions.due_trash(now_iso):
            self.purge_trash_item(item["scene_id"])

    def purge_trash_item(self, scene_id):
        item = self.exclusions.get_trash(scene_id)
        if not item:
            return None
        trash_path = Path(item.get("trash_path") or "")
        deleted = False
        if trash_path and trash_path.exists():
            resolved = ensure_data_path(trash_path, self.data_dir)
            if resolved == self.data_dir or resolved == self.trash_dir:
                raise RuntimeError("Refusing to purge unsafe trash path.")
            if resolved.is_dir():
                shutil.rmtree(resolved)
            else:
                resolved.unlink()
            deleted = True
        self.exclusions.mark_purged(scene_id, deleted_safe=deleted, deleted_zip=deleted)
        return {"scene_id": scene_id, "purged": deleted}

    def empty_trash(self):
        items = self.exclusions.list_trash()
        purged = 0
        for item in items:
            result = self.purge_trash_item(item["scene_id"])
            if result:
                purged += 1
        self.rescan()
        return {"purged": purged, "summary": self.summary}

    def restore_from_trash(self, scene_id):
        item = self.exclusions.get_trash(scene_id)
        if not item:
            return None
        trash_path = ensure_data_path(item["trash_path"], self.data_dir)
        if not trash_path.exists() or not trash_path.is_dir():
            raise RuntimeError("휴지통 파일을 찾을 수 없습니다.")

        restored_safe = restore_from_directory(trash_path, item["safe_path"]) if item.get("safe_path") else False
        restored_zip = restore_from_directory(trash_path, item["zip_path"]) if item.get("zip_path") else False
        if trash_path.exists() and not any(trash_path.iterdir()):
            trash_path.rmdir()
        self.exclusions.remove(scene_id)
        self.rescan()
        return {
            "scene_id": scene_id,
            "restored_safe": restored_safe,
            "restored_zip": restored_zip,
            "summary": self.summary,
        }

    def rescan(self):
        with self.lock:
            self.purge_expired_trash()
            raw_scenes = scan_catalog(self.data_dir)
            excluded_ids = self.exclusions.ids()
            raw_scenes = [scene for scene in raw_scenes if scene["id"] not in excluded_ids]
            raw_summary = summarize(raw_scenes)
            self.scenes = [to_common_scene(scene) for scene in raw_scenes]
            self.summary = {
                "scene_count": len(self.scenes),
                "error_count": 0,
                "excluded_count": self.exclusions.count(),
                "trash_count": self.exclusions.trash_count(),
                "trash_retention_days": TRASH_RETENTION_DAYS,
                "auto_rescan_interval_seconds": AUTO_RESCAN_INTERVAL_SECONDS if AUTO_RESCAN_ENABLED else 0,
                "families": {name: len([s for s in self.scenes if s["satellite_family"] == name]) for name in raw_summary["collections"]},
                "missions": raw_summary["mission_counts"],
                "orbit_directions": raw_summary["direction_counts"],
                "months": raw_summary["month_counts"],
                "date_min": raw_summary["date_start"],
                "date_max": raw_summary["date_end"],
                "total_safe_size": sum(scene["safe_size"] for scene in self.scenes),
                "total_safe_size_label": human_size(sum(scene["safe_size"] for scene in self.scenes)),
                "supported_adapters": ["sentinel1_safe", "kompsat3_directory", "kompsat5_directory"],
                "future_ready": ["Sentinel-2 SAFE", "Landsat Collection", "GeoTIFF scenes"],
                "source_summary": raw_summary,
            }

    def filtered_scenes(self, params):
        with self.lock:
            scenes = list(self.scenes)
        query = params.get("q", [""])[0].lower().strip()
        mission = params.get("mission", [""])[0]
        direction = params.get("direction", [""])[0]
        family = params.get("family", [""])[0]
        date_from = params.get("from", [""])[0]
        date_to = params.get("to", [""])[0]

        if query:
            scenes = [
                scene for scene in scenes
                if query in scene["name"].lower()
                or query in scene["date"].lower()
                or query in scene["relative_orbit"].lower()
                or query in scene["orbit_direction"].lower()
            ]
        if mission:
            scenes = [scene for scene in scenes if scene["mission"] == mission]
        if direction:
            scenes = [scene for scene in scenes if scene["orbit_direction"] == direction]
        if family:
            scenes = [scene for scene in scenes if scene["satellite_family"] == family]
        if date_from:
            scenes = [scene for scene in scenes if scene["date"] >= date_from]
        if date_to:
            scenes = [scene for scene in scenes if scene["date"] <= date_to]
        return scenes

    def scene_by_id(self, scene_id):
        with self.lock:
            for scene in self.scenes:
                if scene["id"] == scene_id:
                    return json.loads(json.dumps(scene))
        return None

    def exclude_to_trash(self, scene_id, reason, actor_email):
        scene = self.scene_by_id(scene_id)
        if not scene:
            return None

        safe_path = ensure_data_path(scene["path"], self.data_dir)
        zip_path = ensure_data_path(scene["zip_path"], self.data_dir) if scene["zip_path"] else None
        trash_root = self.trash_dir / f"{safe_path_part(scene_id)}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        moved_safe = None
        moved_zip = None

        if zip_path and zip_path.exists():
            if zip_path.is_dir():
                raise RuntimeError("Expected zip path is a directory.")
            moved_zip = move_to_directory(zip_path, trash_root)

        if safe_path.exists():
            if not safe_path.is_dir():
                raise RuntimeError("Expected product path is not a directory.")
            moved_safe = move_to_directory(safe_path, trash_root)

        delete_after = (
            datetime.now(timezone.utc) + timedelta(days=TRASH_RETENTION_DAYS)
        ).isoformat(timespec="seconds")

        self.exclusions.add_trash(
            scene,
            reason,
            str(trash_root),
            delete_after,
            actor_email,
        )
        self.rescan()

        return {
            "scene_id": scene_id,
            "trashed_safe": bool(moved_safe),
            "trashed_zip": bool(moved_zip),
            "delete_after": delete_after,
            "trash_retention_days": TRASH_RETENTION_DAYS,
            "summary": self.summary,
        }


class DownloadManager:
    def __init__(self, data_dir, exclusions, catalog_store):
        self.data_dir = Path(data_dir).resolve()
        sentinel_dir = self.data_dir / "Sentinel-1"
        self.download_dir = sentinel_dir if sentinel_dir.exists() else self.data_dir
        self.exclusions = exclusions
        self.catalog_store = catalog_store
        self.lock = threading.Lock()
        self.status = {
            "running": False,
            "phase": "idle",
            "message": "",
            "searched": 0,
            "selected": 0,
            "parallel_downloads": 1,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "current": None,
            "current_bytes": 0,
            "total_bytes": 0,
            "search_bbox": None,
            "logs": [],
            "error": "",
            "started_at": "",
            "finished_at": "",
        }

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.status))

    def log(self, message):
        with self.lock:
            self.status["message"] = message
            self.status["logs"].append(f"{datetime.now().strftime('%H:%M:%S')} {message}")
            self.status["logs"] = self.status["logs"][-80:]

    def update(self, **kwargs):
        with self.lock:
            self.status.update(kwargs)

    def product_state(self, item):
        name = item["Name"]
        scene_id = product_to_scene_id(name)
        zip_path = self.download_dir / product_to_zip_name(name)
        safe_path = self.download_dir / f"{scene_id}.SAFE"
        expected_size = item.get("ContentLength")
        expected_size = int(expected_size) if expected_size is not None else None

        downloaded = False
        if safe_path.exists():
            downloaded = True
        elif zip_path.exists() and zip_path.stat().st_size > 0:
            downloaded = expected_size is None or zip_path.stat().st_size == expected_size

        if self.exclusions.contains(scene_id):
            state = "excluded"
        elif downloaded:
            state = "downloaded"
        else:
            state = "new"

        return {
            "id": item["Id"],
            "scene_id": scene_id,
            "name": name,
            "start": parse_content_start(item.get("ContentDate")),
            "online": item.get("Online"),
            "content_length": expected_size,
            "size": human_size(expected_size),
            "state": state,
        }

    def search(self, days_back=30, top=20, bbox=None, month_from=None, month_to=None):
        items = search_cdse_sentinel1(days_back=days_back, top=top, bbox=bbox, month_from=month_from, month_to=month_to)
        return [self.product_state(item) for item in items]

    def start(self, days_back=30, top=20, max_downloads=None, bbox=None, month_from=None, month_to=None):
        with self.lock:
            if self.status["running"]:
                return False
            self.status.update({
                "running": True,
                "phase": "queued",
                "message": "다운로드 작업이 대기열에 등록되었습니다.",
                "searched": 0,
                "selected": 0,
                "parallel_downloads": 1,
                "downloaded": 0,
                "skipped": 0,
                "failed": 0,
                "current": None,
                "current_bytes": 0,
                "total_bytes": 0,
                "search_bbox": bbox,
                "search_month_from": month_from or "",
                "search_month_to": month_to or "",
                "logs": [],
                "error": "",
                "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "finished_at": "",
            })

        thread = threading.Thread(
            target=self._run,
            args=(days_back, top, max_downloads, bbox, month_from, month_to),
            daemon=True,
        )
        thread.start()
        return True

    def _run(self, days_back, top, max_downloads, bbox, month_from, month_to):
        try:
            self.update(phase="searching")
            if month_from and month_to:
                self.log(f"Sentinel-1 이미지 검색 중: {month_from} ~ {month_to}, 최대 {top}개")
            else:
                self.log(f"Sentinel-1 이미지 검색 중: 최근 {days_back}일, 최대 {top}개")
            if bbox:
                self.log(f"검색 영역 bbox: 서 {bbox[0]}, 남 {bbox[1]}, 동 {bbox[2]}, 북 {bbox[3]}")
            else:
                self.log("검색 영역: 제주 기본값")
            results = self.search(days_back=days_back, top=top, bbox=bbox, month_from=month_from, month_to=month_to)
            candidates = [item for item in results if item["state"] == "new"]
            if max_downloads is not None:
                candidates = candidates[:max_downloads]
            skipped = len(results) - len(candidates)
            self.update(searched=len(results), selected=len(candidates), skipped=skipped)

            if not candidates:
                self.log("다운로드할 신규 이미지가 없습니다.")
                return

            token = get_cdse_token()
            self.log("대기열 모드: 이미지를 한 번에 1개씩 다운로드합니다.")
            for item in candidates:
                self.update(phase="downloading", current=item, current_bytes=0, total_bytes=item["content_length"] or 0)
                self.log(f"{item['name']} 다운로드 중")
                token = self.download_product(item, token)
                self.update(phase="extracting")
                self.extract_product(item)
                self.catalog_store.rescan()
                self.update(downloaded=self.snapshot()["downloaded"] + 1)
                self.log(f"{item['name']} 완료")
        except Exception as error:
            self.update(failed=self.snapshot()["failed"] + 1, error=str(error))
            self.log(f"다운로드 작업 실패: {error}")
        finally:
            self.update(running=False, phase="idle", current=None, finished_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def download_product(self, item, token):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        url = f"{DOWNLOAD_URL}({item['id']})/$value"
        zip_path = self.download_dir / product_to_zip_name(item["name"])
        part_path = zip_path.with_name(zip_path.name + ".part")
        expected_size = item["content_length"]
        chunk_size = 1024 * 1024

        for attempt in range(1, 11):
            resume_byte = part_path.stat().st_size if part_path.exists() else 0
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": "sar-viewer-downloader/1.0",
            }
            if resume_byte:
                headers["Range"] = f"bytes={resume_byte}-"
                self.log(f"{human_size(resume_byte)}부터 이어받기")

            request = Request(url, headers=headers)
            try:
                with urlopen(request, timeout=1800) as response:
                    status_code = response.status
                    mode = "ab" if status_code == 206 else "wb"
                    if resume_byte and status_code == 200:
                        resume_byte = 0
                        mode = "wb"
                    with part_path.open(mode) as file:
                        current = resume_byte
                        while True:
                            chunk = response.read(chunk_size)
                            if not chunk:
                                break
                            file.write(chunk)
                            current += len(chunk)
                            self.update(current_bytes=current)
                final_size = part_path.stat().st_size
                if expected_size is not None and final_size != expected_size:
                    if final_size > expected_size:
                        part_path.unlink()
                        self.update(current_bytes=0)
                        self.log(
                            "받은 파일이 예상보다 큽니다. "
                            "이 이미지를 처음부터 다시 받습니다."
                        )
                    elif attempt < 10:
                        self.log(
                            "다운로드 스트림이 일찍 종료되었습니다. "
                            f"현재 {human_size(final_size)} / 전체 {human_size(expected_size)}"
                        )
                    if attempt == 10:
                        raise RuntimeError(f"다운로드 크기 불일치: {final_size} != {expected_size}")
                    token = get_cdse_token()
                    sleep_sec = min(60, 5 * attempt)
                    self.log(f"{sleep_sec}초 후 재시도")
                    time.sleep(sleep_sec)
                    continue
                break
            except HTTPError as error:
                if error.code == 401 and attempt < 10:
                    self.log("접근 토큰이 만료되어 갱신합니다.")
                    token = get_cdse_token()
                    continue
                raise
            except Exception:
                if attempt == 10:
                    raise
                sleep_sec = min(60, 5 * attempt)
                self.log(f"{sleep_sec}초 후 재시도")
                time.sleep(sleep_sec)

        final_size = part_path.stat().st_size
        if expected_size is not None and final_size != expected_size:
            raise RuntimeError(f"다운로드 크기 불일치: {final_size} != {expected_size}")
        part_path.replace(zip_path)
        return token

    def extract_product(self, item):
        scene_id = item["scene_id"]
        zip_path = self.download_dir / product_to_zip_name(item["name"])
        safe_path = self.download_dir / f"{scene_id}.SAFE"
        if safe_path.exists():
            self.log(f"SAFE가 이미 존재합니다: {safe_path.name}")
            return
        temp_dir = self.download_dir / f".extracting_{scene_id}"
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True)
        self.log(f"{zip_path.name} 압축 해제 중")
        try:
            with zipfile.ZipFile(zip_path) as archive:
                safe_extract_zip(archive, temp_dir)
            extracted_safe = temp_dir / f"{scene_id}.SAFE"
            if not extracted_safe.exists():
                raise RuntimeError("압축 해제된 SAFE 폴더를 찾을 수 없습니다.")
            shutil.move(str(extracted_safe), str(safe_path))
        finally:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)


def make_geojson(scenes):
    features = []
    for scene in scenes:
        if not scene["footprint"]:
            continue
        features.append({
            "type": "Feature",
            "id": scene["id"],
            "geometry": scene["footprint"],
            "properties": {
                "id": scene["id"],
                "name": scene["name"],
                "family": scene["satellite_family"],
                "mission": scene["mission"],
                "date": scene["date"],
                "start_time": scene["start_time"],
                "orbit_direction": scene["orbit_direction"],
                "relative_orbit": scene["relative_orbit"],
                "polarization": scene["polarization"],
                "safe_size_label": scene["safe_size_label"],
                "status": scene["status"]["processing"],
            },
        })
    return {"type": "FeatureCollection", "features": features}


def scene_for_user(scene, user):
    public = json.loads(json.dumps(scene))
    if user.get("role") != "admin":
        public["path"] = ""
        public["zip_path"] = ""
        public.get("preview", {}).pop("quicklook_path", None)
        public.get("preview", {}).pop("thumbnail_path", None)
    return public


def scenes_for_user(scenes, user):
    return [scene_for_user(scene, user) for scene in scenes]


def write_csv_response(handler, scenes):
    fields = [
        "name", "satellite_family", "mission", "sensor", "product_type", "mode",
        "polarization", "date", "start_time", "stop_time", "orbit_direction",
        "relative_orbit", "absolute_orbit", "zip_size_label", "safe_size_label",
        "path",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for scene in scenes:
        writer.writerow({field: scene.get(field, "") for field in fields})
    body = output.getvalue().encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", 'attachment; filename="satellite-scenes.csv"')
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def write_file_response(handler, path):
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.end_headers()
    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)


def write_download_response(handler, path, download_name=None):
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    filename = safe_download_name(download_name or path.name)
    handler.send_response(200)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.end_headers()
    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            handler.wfile.write(chunk)


def write_directory_download_response(handler, directory, download_name=None):
    size = directory_size(directory)
    if size > MAX_ON_DEMAND_ZIP_BYTES:
        raise RuntimeError(f"Directory is too large for on-demand download: {human_size(size)}")
    if not ZIP_SEMAPHORE.acquire(blocking=False):
        raise RuntimeError("Another folder download is being prepared. Please try again shortly.")
    filename = safe_download_name(download_name or f"{directory.name}.zip")
    temp_path = None
    try:
        temp_file = tempfile.NamedTemporaryFile(prefix="sar-viewer-", suffix=".zip", dir=directory.parent, delete=False)
        temp_path = Path(temp_file.name)
        temp_file.close()
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for item in sorted(directory.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(directory.parent))
        write_download_response(handler, temp_path, filename)
    finally:
        ZIP_SEMAPHORE.release()
        if temp_path:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def write_file_head_response(handler, path):
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    handler.send_response(200)
    handler.send_header("Content-Type", mime_type)
    handler.send_header("Cache-Control", "public, max-age=3600")
    handler.send_header("Content-Length", str(path.stat().st_size))
    handler.end_headers()


def read_json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length <= 0:
        return {}
    if length > MAX_JSON_BODY_BYTES:
        raise ValueError("요청 본문이 너무 큽니다.")
    body = handler.rfile.read(length)
    return json.loads(body.decode("utf-8"))


def cookie_header(token, expires_at):
    max_age = max(0, expires_at - int(time.time()))
    secure = "; Secure" if os.environ.get("SAR_VIEWER_COOKIE_SECURE") == "1" else ""
    return (
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={max_age}; "
        f"HttpOnly; SameSite=Lax{secure}"
    )


def clear_cookie_header():
    secure = "; Secure" if os.environ.get("SAR_VIEWER_COOKIE_SECURE") == "1" else ""
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"


def make_handler(store, downloads, auth):
    login_attempts = {}

    class ViewerHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

        def same_origin_post(self):
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            parsed = urlparse(origin)
            return parsed.netloc == host

        def client_key(self):
            forwarded = self.headers.get("X-Forwarded-For", "")
            return forwarded.split(",", 1)[0].strip() or self.client_address[0]

        def login_allowed(self, email):
            now = time.time()
            key = (self.client_key(), email.strip().lower())
            attempts = [stamp for stamp in login_attempts.get(key, []) if now - stamp < 300]
            login_attempts[key] = attempts
            return len(attempts) < 8

        def record_login_failure(self, email):
            key = (self.client_key(), email.strip().lower())
            login_attempts.setdefault(key, []).append(time.time())

        def clear_login_failures(self, email):
            login_attempts.pop((self.client_key(), email.strip().lower()), None)

        def send_json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_auth_json(self, payload, token=None, expires_at=None, clear_session=False, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if token and expires_at:
                self.send_header("Set-Cookie", cookie_header(token, expires_at))
            if clear_session:
                self.send_header("Set-Cookie", clear_cookie_header())
            self.end_headers()
            self.wfile.write(body)

        def session_token(self):
            raw_cookie = self.headers.get("Cookie", "")
            parsed = cookies.SimpleCookie()
            parsed.load(raw_cookie)
            morsel = parsed.get(SESSION_COOKIE)
            return morsel.value if morsel else ""

        def current_user(self):
            return auth.user_for_token(self.session_token())

        def require_user(self):
            user = self.current_user()
            if user:
                return user
            self.send_json({"error": "로그인이 필요합니다."}, status=401)
            return None

        def require_admin(self):
            user = self.require_user()
            if not user:
                return None
            if user.get("role") != "admin":
                self.send_json({"error": "관리자 권한이 필요합니다."}, status=403)
                return None
            return user

        def serve_static(self, path):
            if path == "/":
                path = "/index.html"
            file_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if not is_relative_to(file_path, STATIC_DIR) or not file_path.exists() or not file_path.is_file():
                self.send_error(404)
                return
            body = file_path.read_bytes()
            mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def serve_static_head(self, path):
            if path == "/":
                path = "/index.html"
            file_path = (STATIC_DIR / path.lstrip("/")).resolve()
            if not is_relative_to(file_path, STATIC_DIR) or not file_path.exists() or not file_path.is_file():
                self.send_error(404)
                return
            mime_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path == "/api/auth/me":
                user = self.current_user()
                if not user:
                    self.send_json({"user": None}, status=401)
                    return
                self.send_json({"user": user})
                return
            if parsed.path.startswith("/api/") and not self.require_user():
                return
            if parsed.path == "/api/summary":
                self.send_json({"summary": store.summary, "errors": []})
                return
            if parsed.path == "/api/scenes":
                scenes = store.filtered_scenes(params)
                user = self.require_user()
                if not user:
                    return
                self.send_json({"scenes": scenes_for_user(scenes, user), "count": len(scenes)})
                return
            if parsed.path == "/api/footprints":
                self.send_json(make_geojson(store.filtered_scenes(params)))
                return
            if parsed.path == "/api/export.csv":
                user = self.require_user()
                if not user:
                    return
                write_csv_response(self, scenes_for_user(store.filtered_scenes(params), user))
                return
            if parsed.path == "/api/rescan":
                if not self.require_admin():
                    return
                store.rescan()
                self.send_json({"summary": store.summary, "errors": []})
                return
            if parsed.path == "/api/exclusions":
                if not self.require_admin():
                    return
                self.send_json({"exclusions": store.exclusions.list()})
                return
            if parsed.path == "/api/trash":
                self.send_json({
                    "items": store.exclusions.list_trash(),
                    "retention_days": TRASH_RETENTION_DAYS,
                    "can_empty": self.current_user().get("role") == "admin",
                })
                return
            if parsed.path == "/api/users":
                if not self.require_admin():
                    return
                self.send_json({"users": auth.list_users()})
                return
            if parsed.path == "/api/download/search":
                if not self.require_admin():
                    return
                try:
                    month_from = params.get("from", [""])[0]
                    month_to = params.get("to", [""])[0]
                    days_back = bounded_int(params.get("days", ["30"])[0], 30, 1, MAX_DAYS_BACK, "days")
                    top = bounded_int(params.get("top", ["20"])[0], 20, 1, MAX_SEARCH_TOP, "top")
                    bbox = parse_bbox(params.get("bbox", [""])[0])
                    self.send_json({
                        "products": downloads.search(
                            days_back=days_back,
                            top=top,
                            bbox=bbox,
                            month_from=month_from,
                            month_to=month_to,
                        )
                    })
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                return
            if parsed.path == "/api/download/status":
                if not self.require_admin():
                    return
                self.send_json(downloads.snapshot())
                return
            if parsed.path.startswith("/api/scenes/") and parsed.path.endswith("/download"):
                try:
                    parts = parsed.path.split("/")
                    if len(parts) != 5:
                        self.send_error(404)
                        return
                    scene = store.scene_by_id(parts[3])
                    if not scene:
                        self.send_error(404)
                        return
                    zip_path = ensure_data_path(scene["zip_path"], store.data_dir) if scene.get("zip_path") else None
                    if zip_path and zip_path.exists():
                        write_download_response(self, zip_path, zip_path.name)
                        return
                    data_path = ensure_data_path(scene["path"], store.data_dir) if scene.get("path") else None
                    if data_path and data_path.exists() and data_path.is_dir():
                        write_directory_download_response(self, data_path, f"{scene['id']}.zip")
                        return
                    if data_path and data_path.exists() and data_path.is_file():
                        write_download_response(self, data_path, data_path.name)
                        return
                    self.send_json({"error": "Download files are not available for this scene."}, status=404)
                except RuntimeError as error:
                    self.send_json({"error": str(error)}, status=400)
                return
            if parsed.path.startswith("/api/preview/"):
                parts = parsed.path.split("/")
                if len(parts) != 5 or parts[4] not in {"quick-look.png", "thumbnail.png"}:
                    self.send_error(404)
                    return
                scene = store.scene_by_id(parts[3])
                if not scene:
                    self.send_error(404)
                    return
                preview_key = "quicklook_path" if parts[4] == "quick-look.png" else "thumbnail_path"
                preview_value = scene.get("preview", {}).get(preview_key) or ""
                if not preview_value:
                    self.send_error(404)
                    return
                preview_path = ensure_data_path(preview_value, store.data_dir)
                if not preview_path.exists():
                    self.send_error(404)
                    return
                write_file_response(self, preview_path)
                return
            self.serve_static(parsed.path)

        def do_POST(self):
            parsed = urlparse(self.path)
            parts = parsed.path.split("/")
            if not self.same_origin_post():
                self.send_json({"error": "허용되지 않은 요청 출처입니다."}, status=403)
                return
            if parsed.path == "/api/auth/login":
                try:
                    payload = read_json_body(self)
                    email = str(payload.get("email", ""))
                    password = str(payload.get("password", ""))
                    if not self.login_allowed(email):
                        self.send_json({"error": "로그인 시도가 너무 많습니다. 잠시 후 다시 시도해 주세요."}, status=429)
                        return
                    user = auth.authenticate(email, password)
                    if not user:
                        self.record_login_failure(email)
                        self.send_json({"error": "이메일 또는 비밀번호를 확인해 주세요."}, status=401)
                        return
                    self.clear_login_failures(email)
                    token, expires_at = auth.create_session(user["email"])
                    self.send_auth_json({"user": user}, token=token, expires_at=expires_at)
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if parsed.path == "/api/auth/logout":
                auth.delete_session(self.session_token())
                self.send_auth_json({"ok": True}, clear_session=True)
                return

            user = self.require_user()
            if not user:
                return

            if parsed.path == "/api/users":
                admin = self.require_admin()
                if not admin:
                    return
                try:
                    payload = read_json_body(self)
                    result = auth.create_user(
                        str(payload.get("email", "")),
                        str(payload.get("password", "")),
                        str(payload.get("role", "user") or "user"),
                    )
                    self.send_json({"user": result}, status=201)
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if len(parts) == 5 and parts[1] == "api" and parts[2] == "users" and parts[4] == "update":
                admin = self.require_admin()
                if not admin:
                    return
                try:
                    payload = read_json_body(self)
                    result = auth.update_user(
                        unquote(parts[3]),
                        role=payload.get("role"),
                        active=payload.get("active") if "active" in payload else None,
                        password=str(payload.get("password", "") or ""),
                        actor_email=admin["email"],
                    )
                    self.send_json({"user": result})
                except KeyError as error:
                    self.send_json({"error": str(error)}, status=404)
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if len(parts) == 5 and parts[1] == "api" and parts[2] == "scenes" and parts[4] == "exclude":
                try:
                    payload = read_json_body(self)
                    reason = str(payload.get("reason", "")).strip()
                    result = store.exclude_to_trash(parts[3], reason, user["email"])
                    if not result:
                        self.send_error(404)
                        return
                    self.send_json(result)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if parsed.path == "/api/trash/empty":
                if not self.require_admin():
                    return
                try:
                    self.send_json(store.empty_trash())
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if len(parts) == 5 and parts[1] == "api" and parts[2] == "trash" and parts[4] == "restore":
                try:
                    result = store.restore_from_trash(unquote(parts[3]))
                    if not result:
                        self.send_error(404)
                        return
                    self.send_json(result)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            if parsed.path == "/api/download/start":
                if not self.require_admin():
                    return
                try:
                    payload = read_json_body(self)
                    max_downloads = payload.get("max_downloads")
                    started = downloads.start(
                        days_back=bounded_int(payload.get("days", 30), 30, 1, MAX_DAYS_BACK, "days"),
                        top=bounded_int(payload.get("top", 20), 20, 1, MAX_SEARCH_TOP, "top"),
                        max_downloads=int(max_downloads) if max_downloads not in (None, "") else None,
                        bbox=parse_bbox(payload.get("bbox")),
                        month_from=str(payload.get("from", "") or ""),
                        month_to=str(payload.get("to", "") or ""),
                    )
                    if not started:
                        self.send_json({"error": "다운로드 작업이 이미 실행 중입니다."}, status=409)
                        return
                    self.send_json(downloads.snapshot())
                except ValueError as error:
                    self.send_json({"error": str(error)}, status=400)
                except Exception as error:
                    self.send_json({"error": str(error)}, status=500)
                return

            self.send_error(404)

        def do_HEAD(self):
            parsed = urlparse(self.path)
            if parsed.path.startswith("/api/") and not self.current_user():
                self.send_response(401)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            if parsed.path.startswith("/api/scenes/") and parsed.path.endswith("/download"):
                parts = parsed.path.split("/")
                if len(parts) != 5:
                    self.send_error(404)
                    return
                scene = store.scene_by_id(parts[3])
                if not scene:
                    self.send_error(404)
                    return
                zip_path = ensure_data_path(scene["zip_path"], store.data_dir) if scene.get("zip_path") else None
                data_path = ensure_data_path(scene["path"], store.data_dir) if scene.get("path") else None
                if zip_path and zip_path.exists():
                    download_path = zip_path
                    download_name = zip_path.name
                    content_length = str(zip_path.stat().st_size)
                    mime_type = mimetypes.guess_type(str(zip_path))[0] or "application/octet-stream"
                elif data_path and data_path.exists():
                    download_path = data_path
                    download_name = f"{scene['id']}.zip" if data_path.is_dir() else data_path.name
                    content_length = str(data_path.stat().st_size) if data_path.is_file() else None
                    mime_type = "application/zip" if data_path.is_dir() else mimetypes.guess_type(str(data_path))[0] or "application/octet-stream"
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", mime_type)
                self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
                if content_length:
                    self.send_header("Content-Length", content_length)
                self.end_headers()
                return
            if parsed.path.startswith("/api/preview/"):
                parts = parsed.path.split("/")
                if len(parts) != 5 or parts[4] not in {"quick-look.png", "thumbnail.png"}:
                    self.send_error(404)
                    return
                scene = store.scene_by_id(parts[3])
                if not scene:
                    self.send_error(404)
                    return
                preview_key = "quicklook_path" if parts[4] == "quick-look.png" else "thumbnail_path"
                preview_value = scene.get("preview", {}).get(preview_key) or ""
                if not preview_value:
                    self.send_error(404)
                    return
                preview_path = ensure_data_path(preview_value, store.data_dir)
                if not preview_path.exists():
                    self.send_error(404)
                    return
                write_file_head_response(self, preview_path)
                return
            if parsed.path.startswith("/api/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return
            self.serve_static_head(parsed.path)

    return ViewerHandler


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def start_auto_rescan(store):
    if not AUTO_RESCAN_ENABLED or AUTO_RESCAN_INTERVAL_SECONDS <= 0:
        print("Auto rescan disabled", flush=True)
        return

    def worker():
        while True:
            time.sleep(AUTO_RESCAN_INTERVAL_SECONDS)
            try:
                store.rescan()
                print(
                    "Auto rescan complete: "
                    f"{store.summary.get('scene_count', 0)} scene(s)",
                    flush=True,
                )
            except Exception as error:
                print(f"Auto rescan failed: {error}", flush=True)

    thread = threading.Thread(target=worker, name="auto-rescan", daemon=True)
    thread.start()
    print(f"Auto rescan interval: {AUTO_RESCAN_INTERVAL_SECONDS} seconds", flush=True)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Browser-based satellite image catalog viewer.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="Directory containing satellite products.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="SQLite DB path for viewer state.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind.")
    parser.add_argument("--port", default=8765, type=int, help="Port to bind.")
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    if not data_dir.exists():
        raise SystemExit(f"Data directory does not exist: {data_dir}")

    exclusions = ExclusionStore(args.db_path)
    auth = AuthStore(args.db_path, ADMIN_EMAIL)
    store = CatalogStore(data_dir, exclusions)
    downloads = DownloadManager(data_dir, exclusions, store)
    start_auto_rescan(store)
    server = ReusableThreadingHTTPServer((args.host, args.port), make_handler(store, downloads, auth))
    print(f"Satellite viewer serving {store.summary['scene_count']} scene(s)", flush=True)
    print(f"Local:   http://127.0.0.1:{args.port}", flush=True)
    print(f"Network: http://{local_ip()}:{args.port}", flush=True)
    print(f"Data:    {data_dir}", flush=True)
    print(f"DB:      {Path(args.db_path).expanduser().resolve()}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
