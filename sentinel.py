import os
import time
import sqlite3
import requests
import pandas as pd
from tqdm import tqdm
from pathlib import Path
from datetime import datetime, timedelta, timezone


# ============================================================
# 사용자 설정
# ============================================================

# CDSE 계정 정보는 터미널 환경변수로 설정 권장
# export CDSE_USERNAME="your_email@example.com"
# export CDSE_PASSWORD="your_password"
USERNAME = os.environ.get("CDSE_USERNAME")
PASSWORD = os.environ.get("CDSE_PASSWORD")

if not USERNAME or not PASSWORD:
    raise RuntimeError(
        "CDSE_USERNAME 또는 CDSE_PASSWORD 환경변수가 설정되지 않았습니다.\n"
        "터미널에서 아래처럼 설정하세요:\n\n"
        "export CDSE_USERNAME='your_email@example.com'\n"
        "export CDSE_PASSWORD='your_password'\n"
    )

# 저장 폴더는 절대경로로 고정하는 것을 추천
# 본인 환경에 맞게 수정하세요.
OUT_DIR = "/Users/ai_mac/Platform/sar_viewer/sentinel1_jeju"
EXCLUSION_DB = os.environ.get(
    "SAR_VIEWER_DB",
    "/Users/ai_mac/Platform/sar_viewer/viewer_state/sar_viewer.sqlite3",
)

# InSAR / 지반침하 분석용 Sentinel-1 SLC
PRODUCT_TYPE = "IW_SLC__1S"

# 최근 며칠 이내 자료 검색
DAYS_BACK = 360

# 테스트 단계에서는 1개만 권장
MAX_DOWNLOADS = 100

# 검색 결과 개수
SEARCH_TOP = 100

# 제주도 전체를 넉넉히 포함하는 WKT polygon
# 좌표 순서: longitude latitude
JEJU_WKT = (
    "POLYGON(("
    "126.05 33.05,"
    "127.05 33.05,"
    "127.05 33.75,"
    "126.05 33.75,"
    "126.05 33.05"
    "))"
)

# CDSE API 주소
CATALOGUE_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_URL = "https://download.dataspace.copernicus.eu/odata/v1/Products"
TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/"
    "protocol/openid-connect/token"
)


# ============================================================
# Access Token 발급
# ============================================================

def get_access_token(username: str, password: str) -> str:
    data = {
        "client_id": "cdse-public",
        "username": username,
        "password": password,
        "grant_type": "password",
    }

    response = requests.post(TOKEN_URL, data=data, timeout=60)

    if response.status_code != 200:
        print("Token request failed.")
        print("Status code:", response.status_code)
        print("Response:", response.text)
        response.raise_for_status()

    return response.json()["access_token"]


# ============================================================
# Sentinel-1 제품 검색
# ============================================================

def search_sentinel1_jeju(
    product_type: str,
    days_back: int = 30,
    top: int = 20,
) -> pd.DataFrame:
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days_back)

    start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str = end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    odata_filter = (
        "Collection/Name eq 'SENTINEL-1' "
        f"and Attributes/OData.CSC.StringAttribute/any(att:att/Name eq 'productType' "
        f"and att/OData.CSC.StringAttribute/Value eq '{product_type}') "
        f"and ContentDate/Start gt {start_str} "
        f"and ContentDate/Start lt {end_str} "
        f"and OData.CSC.Intersects(area=geography'SRID=4326;{JEJU_WKT}')"
    )

    params = {
        "$filter": odata_filter,
        "$orderby": "ContentDate/Start desc",
        "$top": top,
        "$select": (
            "Id,Name,ContentDate,PublicationDate,Online,"
            "ContentLength,S3Path,GeoFootprint"
        ),
    }

    response = requests.get(CATALOGUE_URL, params=params, timeout=120)

    if response.status_code != 200:
        print("Search request failed.")
        print("Status code:", response.status_code)
        print("URL:", response.url)
        print("Response:", response.text)
        response.raise_for_status()

    items = response.json().get("value", [])

    if not items:
        return pd.DataFrame()

    df = pd.DataFrame(items)

    # ContentDate 표시용 컬럼 추가
    def get_start_date(x):
        if isinstance(x, dict):
            return x.get("Start")
        return None

    df["StartDate"] = df["ContentDate"].apply(get_start_date)

    return df


# ============================================================
# 파일명 생성
# ============================================================

def product_to_zip_name(product_name: str) -> str:
    safe_name = product_name.replace(".SAFE", "")
    return f"{safe_name}.zip"


def product_to_scene_id(product_name: str) -> str:
    return product_name.replace(".SAFE", "")


def is_product_excluded(product_name: str, db_path: str = EXCLUSION_DB) -> bool:
    path = Path(db_path).expanduser()
    if not path.exists():
        return False

    scene_id = product_to_scene_id(product_name)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM excluded_scenes WHERE scene_id = ?",
            (scene_id,),
        ).fetchone()
    return row is not None


def is_product_downloaded(
    product_name: str,
    out_dir: str,
    expected_size: int | None = None,
) -> bool:
    zip_name = product_to_zip_name(product_name)
    out_path = os.path.join(out_dir, zip_name)

    if not os.path.exists(out_path):
        return False

    local_size = os.path.getsize(out_path)

    if expected_size is None:
        return local_size > 0

    return local_size == expected_size


# ============================================================
# Sentinel-1 제품 다운로드
# ============================================================

def download_product(
    product_id: str,
    product_name: str,
    token: str,
    out_dir: str,
    expected_size: int | None = None,
) -> str:
    os.makedirs(out_dir, exist_ok=True)

    url = f"{DOWNLOAD_URL}({product_id})/$value"

    zip_name = product_to_zip_name(product_name)
    out_path = os.path.join(out_dir, zip_name)
    part_path = out_path + ".part"

    # --------------------------------------------------------
    # 1. 이미 완성된 zip 파일이 있으면 스킵
    # --------------------------------------------------------
    if os.path.exists(out_path):
        local_size = os.path.getsize(out_path)

        if expected_size is not None:
            if local_size == expected_size:
                print(f"Already downloaded, skip:")
                print(f"  {out_path}")
                return out_path
            else:
                print("Existing zip file size mismatch.")
                print(f"  file    : {out_path}")
                print(f"  local   : {local_size}")
                print(f"  expected: {expected_size}")
                backup_path = out_path + ".bak"
                print(f"Rename existing file to backup:")
                print(f"  {backup_path}")
                os.rename(out_path, backup_path)
        else:
            if local_size > 0:
                print(f"Existing zip file found, skip:")
                print(f"  {out_path}")
                return out_path

    # --------------------------------------------------------
    # 2. part 파일이 이미 완성 크기와 같으면 zip으로 변경
    # --------------------------------------------------------
    if os.path.exists(part_path) and expected_size is not None:
        part_size = os.path.getsize(part_path)

        if part_size == expected_size:
            os.replace(part_path, out_path)
            print(f"Completed part file renamed to zip:")
            print(f"  {out_path}")
            return out_path

    max_retries = 10
    chunk_size = 1024 * 1024  # 1 MB

    headers_base = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "sentinel1-jeju-downloader/1.0",
    }

    # --------------------------------------------------------
    # 3. 다운로드 반복
    # --------------------------------------------------------
    for attempt in range(1, max_retries + 1):
        resume_byte = os.path.getsize(part_path) if os.path.exists(part_path) else 0

        headers = headers_base.copy()

        if resume_byte > 0:
            headers["Range"] = f"bytes={resume_byte}-"
            print(f"Resume download from {resume_byte / 1024**3:.2f} GB")

        try:
            with requests.get(
                url,
                headers=headers,
                stream=True,
                allow_redirects=True,
                timeout=(60, 1800),
            ) as response:

                # 200: 처음부터 다운로드
                # 206: Range 이어받기 성공
                if resume_byte > 0 and response.status_code == 200:
                    print("Server ignored Range header. Restarting from zero.")
                    resume_byte = 0
                    mode = "wb"

                elif response.status_code == 206:
                    mode = "ab"

                elif response.status_code == 200:
                    mode = "wb"

                else:
                    print("Download request failed.")
                    print("Status code:", response.status_code)
                    print("Response:", response.text[:500])

                    if response.status_code == 401 and attempt < max_retries:
                        print("Access token expired. Refreshing token...")
                        token = get_access_token(USERNAME, PASSWORD)
                        headers_base["Authorization"] = f"Bearer {token}"
                        continue

                    response.raise_for_status()

                content_length = int(response.headers.get("content-length", 0))

                if expected_size is not None:
                    total_size = expected_size
                elif content_length:
                    total_size = resume_byte + content_length
                else:
                    total_size = None

                with open(part_path, mode) as file, tqdm(
                    total=total_size,
                    initial=resume_byte,
                    unit="B",
                    unit_scale=True,
                    desc=product_name[:55],
                ) as progress:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            file.write(chunk)
                            progress.update(len(chunk))

            # ------------------------------------------------
            # 4. 다운로드 완료 후 파일 크기 검증
            # ------------------------------------------------
            final_size = os.path.getsize(part_path)

            if expected_size is not None and final_size != expected_size:
                raise RuntimeError(
                    "Downloaded size mismatch.\n"
                    f"  downloaded: {final_size}\n"
                    f"  expected  : {expected_size}\n"
                    f"  file      : {part_path}"
                )

            os.replace(part_path, out_path)

            print("Download completed:")
            print(f"  {out_path}")

            return out_path

        except KeyboardInterrupt:
            print("\nDownload interrupted by user.")
            print("Partial file kept:")
            print(f"  {part_path}")
            raise

        except Exception as error:
            print(f"\nDownload failed on attempt {attempt}/{max_retries}")
            print(f"Reason: {error}")

            if attempt == max_retries:
                print("Max retries reached. Download failed.")
                raise

            sleep_sec = min(60, 5 * attempt)
            print(f"Retrying after {sleep_sec} seconds...")
            time.sleep(sleep_sec)

    return out_path


# ============================================================
# 메인 실행
# ============================================================

def main():
    print("Searching Sentinel-1 products over Jeju...")

    df = search_sentinel1_jeju(
        product_type=PRODUCT_TYPE,
        days_back=DAYS_BACK,
        top=SEARCH_TOP,
    )

    if df.empty:
        print("No products found.")
        print("Try increasing DAYS_BACK or checking PRODUCT_TYPE.")
        return

    print("\nFound products:")

    display_cols = [
        "Name",
        "StartDate",
        "Online",
        "ContentLength",
    ]

    print(df[display_cols].head(SEARCH_TOP).to_string())

    # 이미 다운로드된 제품은 제외하고, 아직 없는 제품만 최신순으로 선택
    os.makedirs(OUT_DIR, exist_ok=True)
    pending_rows = []
    skipped_count = 0

    for _, row in df.iterrows():
        product_name = row["Name"]
        expected_size = int(row["ContentLength"]) if pd.notnull(row["ContentLength"]) else None

        if is_product_excluded(product_name):
            skipped_count += 1
            print(f"Excluded by viewer DB, skip selecting: {product_name}")
            continue

        if is_product_downloaded(
            product_name=product_name,
            out_dir=OUT_DIR,
            expected_size=expected_size,
        ):
            skipped_count += 1
            print(f"Already downloaded, skip selecting: {product_name}")
            continue

        pending_rows.append(row)

        if len(pending_rows) >= MAX_DOWNLOADS:
            break

    if not pending_rows:
        print("\nNo new products to download.")
        print(f"Already downloaded products skipped: {skipped_count}")
        return

    selected = pd.DataFrame(pending_rows)

    print(f"\nSelected {len(selected)} product(s) for download.")
    print(f"Already downloaded products skipped: {skipped_count}")
    print(f"Output directory: {OUT_DIR}")

    print("\nGetting access token...")
    token = get_access_token(USERNAME, PASSWORD)

    for _, row in selected.iterrows():
        product_id = row["Id"]
        product_name = row["Name"]
        expected_size = int(row["ContentLength"]) if pd.notnull(row["ContentLength"]) else None

        print("\n------------------------------------------------------------")
        print(f"Downloading: {product_name}")
        print(f"Expected size: {expected_size}")
        print("------------------------------------------------------------")

        path = download_product(
            product_id=product_id,
            product_name=product_name,
            token=token,
            out_dir=OUT_DIR,
            expected_size=expected_size,
        )

        print(f"Saved to: {path}")


if __name__ == "__main__":
    main()
