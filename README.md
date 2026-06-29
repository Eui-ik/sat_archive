# Satellite Image Archive

브라우저 기반 위성 이미지 아카이브 플랫폼입니다. Sentinel-1, KOMPSAT-3, KOMPSAT-5, Cas-1 데이터를 지도 위에 표시하고, 이미지 목록/상세 정보/파일 다운로드/휴지통/사용자 관리를 제공합니다.

## 주요 기능

- Sentinel-1, KOMPSAT-3, KOMPSAT-5, Cas-1 이미지 카탈로그 자동 스캔
- 위성 이미지 footprint 지도 표시
- 미리보기 이미지, 촬영일, 궤도, 센서, 용량 등 메타데이터 확인
- 사용자 로그인 및 관리자/일반 사용자 권한 관리
- 일반 사용자 이미지 제외 및 휴지통 이동
- 일반 사용자 휴지통 복구
- 관리자 휴지통 비우기 및 사용자 관리
- 관리자 Sentinel-1 신규 이미지 검색/다운로드
- Docker 기반 실행

## 데이터 구조

기본 Docker 설정은 `/Volumes/SAT`를 컨테이너의 `/data`로 마운트합니다.

```text
/Volumes/SAT
├── Sentinel-1
├── Kompsat3
├── Kompsat5
└── Cas1
```

휴지통 파일은 `/Volumes/SAT/.trash` 아래에 보관됩니다. 기본 보관 기간은 30일입니다.

새 제품 폴더를 데이터 위치에 복사하면 플랫폼이 10분마다 자동 재스캔하여 반영합니다. 복사 중인 제품은 임시 파일이 있거나 최근 수정된 상태로 판단되면 다음 스캔까지 목록에 올리지 않습니다.

## 실행

`.env` 파일을 만들고 필요한 값을 설정합니다.

```bash
SAR_VIEWER_ADMIN_PASSWORD=your_admin_password
CDSE_USERNAME=your_cdse_email
CDSE_PASSWORD=your_cdse_password
```

Docker로 실행합니다.

```bash
docker compose up -d --build
```

브라우저에서 접속합니다.

```text
http://sat.innopam.net
```

기본 관리자 이메일은 다음 값입니다.

```text
euiik@innopam.com
```

## 환경변수

| 이름 | 설명 | 기본값 |
| --- | --- | --- |
| `SAR_VIEWER_PORT` | 웹 서버 포트 | `8765` |
| `SAR_VIEWER_ADMIN_EMAIL` | 시스템 관리자 이메일 | `euiik@innopam.com` |
| `SAR_VIEWER_ADMIN_PASSWORD` | 관리자 비밀번호 | 없음 |
| `SAR_VIEWER_TRASH_RETENTION_DAYS` | 휴지통 보관 기간 | `30` |
| `SAR_VIEWER_AUTO_RESCAN_INTERVAL_SECONDS` | 자동 재스캔 간격 | `600` |
| `SAR_VIEWER_STABLE_PRODUCT_SECONDS` | 복사 중으로 볼 최근 수정 시간 | `120` |
| `CDSE_USERNAME` | Copernicus Data Space 계정 | 없음 |
| `CDSE_PASSWORD` | Copernicus Data Space 비밀번호 | 없음 |

## 개발 실행

Docker 없이 직접 실행할 수도 있습니다.

```bash
python3 sentinel_viewer/app.py --data-dir /Volumes/SAT --host 0.0.0.0 --port 8765
```

## Git 주의사항

위성 원본 데이터와 로컬 DB는 Git에 올리지 않습니다.

- `/Volumes/SAT` 데이터
- `.env`
- `viewer_state/`
- `*.sqlite3`
- `__pycache__/`
