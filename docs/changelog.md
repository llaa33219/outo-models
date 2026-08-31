# 변경 이력

`outo-models` 의 모든 공개 인터페이스 변경은 이 파일에 기록합니다. AGENTS.md §2.8
에 따라 CLI 플래그 / REST 엔드포인트 / 환경 변수는 하위 호환을 유지하고,
깨야 할 때는 마이그레이션 가이드를 함께 제공합니다.

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
