# 변경 이력

`outo-models` 의 모든 공개 인터페이스 변경은 이 파일에 기록합니다. AGENTS.md §2.8
에 따라 CLI 플래그 / REST 엔드포인트 / 환경 변수는 하위 호환을 유지하고,
깨야 할 때는 마이그레이션 가이드를 함께 제공합니다.

## v0.2.0 — LFS · S3 · Spaces 런타임

출시일: 2026-09-01

v1 의 메타데이터 중심 스코프를 실제 운영 가능한 수준으로 끌어올린 v2 릴리즈입니다.
기존 v0.1.0 의 모든 공개 인터페이스는 그대로 동작합니다.

### 추가된 기능

#### Git LFS 정식 지원

이전 버전에서 501 스텬이던 LFS 가 완전한 구현으로 교체되었습니다.

- 4개 엔드포인트 동작:
  - `POST /{owner}/{name}.git/info/lfs/objects/batch` — 업로드/다운로드 action URL
    발급
  - `PUT /{owner}/{name}.git/info/lfs/objects/{oid}` — 스트리밍 업로드
  - `GET /{owner}/{name}.git/info/lfs/objects/{oid}` — 64 KiB 청크 스트리밍
- 인증: 일반 clone/push 와 같은 Basic PAT 재사용 (`git-lfs` 가 자동으로 처리)
- per-object 에러: 한 객체의 실패 (사이즈 / 쿼터 / 404) 가 batch 전체를
  실패시키지 않음 — LFS 스펙 그대로
- SHA256 + 사이즈 검증 후 원자적 rename (`os.replace`)
- symlink 차단: `_object_path` 의 어떤 segment 라도 symlink 이면 읽기/쓰기
  거부 (path traversal 의 입구 차단)
- `/info/lfs/locks*` 만 501 유지 — v3 작업
- 자세한 흐름: [git-repos.md §LFS 사용법](git-repos.md#lfs-사용법-v2)

#### LFS 백엔드: `local` / `s3` 선택 가능

[`OUTO_LFS_BACKEND`](cli.md#환경-변수) 로 두 백엔드를 선택할 수 있습니다.

- `local` (기본): `OUTO_DATA_DIR` 의 `lfs/<aa>/<bb>/<oid>` 에 샤딩 저장. 별도
  인프라 없이 동작.
- `s3`: AWS S3 / MinIO / R2 등 S3 호환 스토리지에 presigned URL 로 직접
  업/다운로드. 자체 구현 SigV4 (path-style, MinIO 호환) — `boto3` /
  `aioboto3` 의존성 없음. 자세한 설정:
  [git-repos.md §백엔드 설정](git-repos.md#백엔드-설정-outo_lfs_backend),
  [security.md §`s3` 백엔드의 presigned URL](security.md#s3-백엔드의-presigned-url).

추가 환경 변수:

- `OUTO_LFS_MAX_OBJECT_BYTES` (기본 5 GiB)
- `OUTO_S3_ENDPOINT`, `OUTO_S3_BUCKET`, `OUTO_S3_REGION` (기본 `us-east-1`)
- `OUTO_S3_ACCESS_KEY`, `OUTO_S3_SECRET_KEY`
- `OUTO_S3_PREFIX` (기본 `lfs`)
- `OUTO_S3_PRESIGN_TTL_SECONDS` (기본 3600)

#### Spaces 런타임 (Podman)

[`src/outo_models/spaces/`](../src/outo_models/spaces) 의 v2 런타임:

- 기본 비활성. `OUTO_SPACES_RUNTIME_ENABLED=true` 로 명시적 활성화.
- `OUTO_PODMAN_SOCKET` (기본 `/run/podman/podman.sock`) 로 Podman REST API 에
  접속. 컨테이너 안에서 Unix 도메인 소켓 (`httpx.AsyncHTTPTransport(uds=...)`) 으로
  통신.
- 라이프사이클: `start` / `stop` / `restart` / `status` + 감사 로그
  (`space.start` / `space.stop` / `space.restart`).
- 컨테이너 이름 규칙 `outo-space-<owner>-<name>`, 이미지 태그
  `localhost/outo-space-<owner>-<name>:latest`, 레이블 `outo.managed=true` +
  `outo.space=<owner>/<name>`.
- 호스트 포트는 `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` (기본 20000..21000)
  에서 순차 할당. 컨테이너 안 포트는 `8000/tcp` 으로 고정, `127.0.0.1` 바인딩
  (외부 노출 금지).
- GPU: `web_settings(key="gpu:<username>")` 의 JSON 배열을
  `nvidia.com/gpu=<id>` CDI 디바이스로 부착.
- SDK 별 동작:
  - `static` — 컨테이너 없이 dulwich 트리를 `spaces/<owner>/<name>/site/` 에
    풀어 `FileResponse` 로 서빙.
  - `gradio` / `streamlit` — 사용자가 저장소 안에 베이스 이미지를 정의한다는
    약속; 코드 측은 docker SDK 와 동일.
  - `docker` — 저장소 루트에 `Dockerfile` 또는 `Containerfile` 이 **없으면**
    `ValidationFailedError` 로 거절.
- 프록시 `/spaces/<owner>/<name>/run/{path}` — 5개 메서드 (GET/POST/PUT/
  PATCH/DELETE) 모두 지원. 컨테이너 running 일 때만 `http://127.0.0.1:<port>/<path>`
  로 위임. hop-by-hop 헤더 제거.

#### 라이선스

[LICENSE](../LICENSE) (Apache-2.0) 추가. v0.1.0 까지는 라이선스 파일이 없어서
재배포가 모호했는데, v0.2.0 부터 Apache-2.0 으로 명확히 합니다.

#### CI / 이미지 릴리즈 워크플로우

두 개의 GitHub Actions 가 추가되었습니다.

- `.github/workflows/ci.yml` — main / PR 트리거. ruff + mypy + pytest +
  `scripts/check-docs.sh` 까지 강제.
- `.github/workflows/release-image.yml` — `vX.Y.Z-stable` / `vX.Y.Z-dev` 태그
  트리거. tests 통과 후 `podman build --build-arg IMAGE_FLAVOR=stable|dev ...`
  로 빌드하고 `ghcr.io/<repo>:X.Y.Z-<flavor>`, `:stable` / `:dev`, 그리고
  stable 인 경우 `:latest` 까지 push. 자세한 태그 컨벤션:
  [architecture.md §CI/CD](architecture.md#cicd).

### 추가된 환경 변수 요약

`OUTO_LFS_BACKEND`, `OUTO_LFS_MAX_OBJECT_BYTES`, `OUTO_S3_ENDPOINT`,
`OUTO_S3_BUCKET`, `OUTO_S3_REGION`, `OUTO_S3_ACCESS_KEY`, `OUTO_S3_SECRET_KEY`,
`OUTO_S3_PREFIX`, `OUTO_S3_PRESIGN_TTL_SECONDS`, `OUTO_SPACES_RUNTIME_ENABLED`,
`OUTO_PODMAN_SOCKET`, `OUTO_SPACES_RUNTIME_PORT_RANGE_START`,
`OUTO_SPACES_RUNTIME_PORT_RANGE_END`. 모두 기본값이 있어 마이그레이션 없이
업그레이드 가능합니다.

### 마이그레이션 가이드

v0.2.0 으로의 업그레이드는 **마이그레이션 절차가 필요 없습니다**. 모든 새 환경
변수의 기본값은 v0.1.0 의 동작과 호환됩니다 (LFS 는 여전히 v0.1.0 의 501 +
로드맵 안내를 그대로 반환하지 않으며, v0.2.0 부터는 실제 LFS 가 동작합니다 —
이는 **기능 추가** 이고 기존 동작의 변경은 아닙니다).

> **주의**: v0.1.0 의 LFS 501 응답에 의존하던 클라이언트 (예: 자체 작성한
> 다운로드 스크립트) 는 v0.2.0 에서 정상 LFS 응답을 받게 됩니다. LFS 비활성화가
> 필요한 운영 환경은 `OUTO_LFS_BACKEND` 를 빈 문자열 대신 `local` 로 두고
> 프록시에서 차단해 주세요. LFS 자체를 끄는 플래그는 제공하지 않습니다.

새 컨테이너를 띄울 때 Spaces 런타임을 활성화하지 않으면 (기본값) 모든 Space 는
v0.1.0 처럼 동작합니다 — `runtime.state = "disabled"`, `/run/` 접근 시
`503 runtime_disabled`.

## v0.1.0 — 첫 출시

출시일: 2026-08-31

v1 의 첫 번째 출시. Hugging Face / ModelScope 스타일의 git 기반 모델 허브를
자체 호스팅하기 위한 모든 핵심 기능이 포함되어 있습니다.

### 설치 / 운영

- `outo-models` Typer 콘솔 스크립트 단일 진입점 (`pyproject.toml` 의
  `console_scripts`)
- 대화형 / 비대화형 설정 마법사 (`outo-models setup`)
- 컨테이너 라이프사이클: `start` / `stop` / `restart` / `status`
- 이미지 갱신 + DB 마이그레이션 + 재시작 (`outo-models update`)
- `outo-models reset` 의 3회 `yes` 게이트 (dry-run 기본, `OUTO_DESTRUCTIVE=1`
  필요)
- Podman 단일 이미지 (`outo-models:stable`, `outo-models:dev`) — 비특권 실행
- AGENTS.md §4 강제: `dev + production` 조합 거부
- quadlet systemd 유닛 예시 + 호스트 측 방화벽 자동 개방 유닛 (opt-in)
- 호스트 측 스크립트 (`firewall-open.sh`, `update.sh`, `reset.sh`)

### DNS / TLS

- DNS 제공자 추상화 + Cloudflare / Manual 구현체
- Cloudflare 모드: DNS A 레코드 자동 생성 + DNS-01 ACME 챌린지
- 수동 모드: 한국어 안내 + 운영자 확인 대기
- Caddy (in-container, 80/443) + 자동 ACME 발급 / 갱신
- `acme-staging-v02.api.letsencrypt.org/directory` 로의 스테이징 전환 지원
  (`TlsConfig.staging = True`)
- 매일 `cert_renewal_job` (00:00 UTC) 가 인증서 점검 + Caddy nudge

### 데이터 / DB

- SQLAlchemy 2.x async + aiosqlite (기본) / Postgres 호환 (DB URL 만 변경)
- Alembic 마이그레이션 (`alembic upgrade head`)
- 9개 테이블: `users`, `repos`, `revisions`, `personal_access_tokens`,
  `approvals`, `user_quotas`, `user_usages`, `audit_logs`, `web_settings`
- per-repo `asyncio.Lock` 으로 동시 푸시 직렬화
- 매시간 `quota_reconcile_job` 으로 사용자 사용량 보정
- 매일 `audit_prune_job` (02:00 UTC) 으로 90일 이전 감사 로그 삭제

### 인증 / 권한

- argon2id 비밀번호 해시 (`time_cost=3`, `memory_cost=64 MiB`)
- PASETO v4 local PAT (평문 미저장, argon2id 지문만 저장)
- itsdangerous URLSafeTimedSerializer 세션 쿠키 (`outo_session`)
- 7일 세션 + 로그인마다 rotation
- CSRF double-submit 쿠키 (UI 폼)
- slowapi 레이트 리밋: login `5/minute`, signup `3/minute`, git push/pull,
  API 모두 per-IP / per-user 버킷
- 보안 헤더 자동 부착: HSTS, CSP, X-Frame-Options, Permissions-Policy 등

### git / REST

- FastAPI + REST 라우터: `auth`, `users`, `repos`, `spaces`, `admin`,
  `webhooks`, UI
- dulwich 기반 git smart-HTTP — URL: `https://<도메인>/<owner>/<name>.git`
- HTTP Basic auth = username + PAT
- 권한 매트릭스: PUSH 는 owner / admin 만, PULL 은 public 은 익명,
  private 은 owner / admin
- 쿼터 초과 → `413`, LFS → `501` (스텁 + 로드맵 링크)
- 푸시 후 `Revision` 기록 + `Repo.size_bytes` 갱신 + `UserUsage` 정합 +
  `AuditLog(action="repo.push")` 기록

### 모델 / 데이터셋 / Spaces

- `RepoKind`: `model` / `dataset` / `space`
- `Visibility`: `public` / `private`
- Spaces v1: 메타데이터 + 정적 페이지 + `SUPPORTED_SDKS = {static, gradio,
  streamlit, docker}`. 런타임은 `preview_unavailable` (v2 로드맵)

### 관리 기능

- 가입 흐름: `pending` → `approved` / `denied` (+ `unban` 으로 `banned` →
  `approved`)
- `admin list` / `pending` / `approve` / `deny` / `ban` / `unban` /
  `reset-password`
- 사용자별 저장 용량 쿼터 (`quota show` / `set`; `10GiB` 형식 입력)
- GPU ID 자유 라벨 할당 (`gpu show` / `assign` / `clear`)
- `--api-url` + `--token` 으로 원격 서버의 `/api/admin/*` 위임 (단,
  `reset-password` 는 로컬 전용)

### 문서 / 자동화

- `docs/` 디렉터리의 한국어 문서 (본 변경 이력 포함)
- `scripts/check-docs.sh` 가 CLI 명령 / 환경 변수 / 문서 일치를 자동 검증
  (`make lint` 처럼 CI 게이트로 사용 가능)
- 758+ pytest 케이스 (단위 + 통합, 컨테이너 없이 실행)
- ruff + mypy strict + bandit 정적 분석

### 알려진 제약

- LFS (`git lfs`) 는 501 스텁만 — 대용량 객체는 별도 분할 권장
- Spaces 컨테이너 런타임 미지원 — `runtime.state` 항상 `preview_unavailable`
- Webhook 엔드포인트는 `/api/webhooks/test` 만 — 정식 통합은 v2
- `dev` 이미지의 `debugpy` / `ipython` 노출은 의도적 (개발용 이미지에서만)
- 자동 업데이트는 quadlet 의 `AutoUpdate=registry` 정책에 의존 — 호스트의
  `podman-auto-update.timer` 활성화 필요

### 마이그레이션 가이드

v0.1.0 은 첫 출시이므로 별도 마이그레이션 절차는 없습니다. v0.0.x 가
존재하지 않습니다.

## 다음 버전의 방향 (로드맵)

- LFS 정식 지원 (`git lfs` API + 청크 저장소)
- Spaces v2 런타임 (컨테이너 격리 + 빌드 큐 + 자원 제한)
- Webhook 정식 통합 (push / repo.created / user.signup 이벤트)
- 메트릭 / Prometheus exporter
- 자동 업데이트 안정화 (in-place 마이그레이션)

각 항목이 출시될 때 본 변경 이력에 마이너 / 메이저 버전을 올려 추가합니다.
