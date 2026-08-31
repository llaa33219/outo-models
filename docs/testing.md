# 테스트

`outo-models` 는 **개발 환경** 과 **테스트 환경** 을 의도적으로 분리합니다
(AGENTS.md §4). 이 페이지에서는 그 분리가 왜 필요한지, 어떤 명령으로 무엇을
검증하는지를 정리합니다.

## 1. 개발 환경 vs 테스트 환경

### 개발 환경 (현재 작업 머신)

- podman 이 **없음** — 이미지 빌드 / 컨테이너 실행 불가
- 통합 테스트는 **컨테이너 없이** 실행됨 — 실제 `git` 바이너리와 `httpx`
  기반의 in-process 시뮬레이션
- 검증 범위:
  - `uv sync` — 의존성 잠금 일치
  - `make lint` — ruff 린트 + 포맷
  - `make typecheck` — mypy strict
  - `make test` — 단위 + 통합 테스트 (758+)

### 테스트 환경 (별도 머신)

- podman 4.x 설치
- 실제 컨테이너 안에서 `setup` → `start` → `update` → `reset` 흐름 수행
- `make build-stable` / `make build-dev` 로 이미지 빌드 검증
- `hadolint` 등 정적 검증 ([Containerfile](../Containerfile) 의 정적 검토
  코멘트 참고)

## 2. `make` 명령

[Makefile](../Makefile) 의 모든 타겟:

| 명령 | 무엇을 하나 |
| --- | --- |
| `make sync` | `uv sync --frozen` — 의존성 잠금 재설치 |
| `make lint` | `ruff check .` + `ruff format --check .` |
| `make format` | `ruff format .` + `ruff check --fix .` |
| `make typecheck` | `mypy src` (strict 모드) |
| `make test` | `pytest` (단위 + 통합) |
| `make smoke` | `pytest tests/integration/test_e2e_smoke.py -v` |
| `make build-stable` | `podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .` |
| `make build-dev` | `podman build --build-arg IMAGE_FLAVOR=dev -t outo-models:dev .` |

CI 에서는 최소한 `lint`, `typecheck`, `test` 를 항상 통과해야 합니다.
`smoke` 는 통합 테스트의 일부이지만 시간이 더 걸리므로 CI 에서 분리해
실행해도 됩니다.

## 3. 테스트 디렉토리 구조

```
tests/
├── conftest.py                    전역 픽스처 (Settings, async engine, tmp dirs)
├── unit/
│   ├── test_config.py             Settings + env vars
│   ├── test_passwords.py          argon2id 래퍼
│   ├── test_tokens.py             PASETO v4 + fingerprint
│   ├── test_sessions.py           itsdangerous 세션
│   ├── test_rate_limit.py         slowapi 키 함수 + 한도
│   ├── test_hashing.py            utils.hashing
│   ├── test_paths.py              utils.paths
│   ├── test_slug.py               utils.slug
│   ├── test_time.py               utils.time
│   ├── test_logging.py            structlog 설정
│   ├── test_dns_base.py           DNSProvider ABC
│   ├── test_dns_cloudflare.py     CloudflareProvider (respx 기반 mock)
│   ├── test_dns_factory.py        create_provider 디스패치
│   ├── test_dns_manual.py         ManualProvider
│   ├── test_firewall_detect.py    detect_firewall / is_port_open
│   ├── test_firewall_open_ports.py  open_ports + argv 빌드
│   ├── test_caddy_manager.py      Caddyfile 렌더링 + reload
│   ├── test_tls_renewal.py        check_cert_health + renewal_job
│   ├── test_audit_prune.py        prune_audit_logs
│   ├── test_models_*.py           각 ORM 모델
│   ├── test_spaces_runtime.py     RuntimeState / Status
│   ├── test_repos_*.py            create, delete, quota, reflog, storage
│   ├── test_git_smart_auth.py     Basic auth + authorize 매트릭스
│   ├── test_git_smart_lfs.py      LFS 501 스텁
│   ├── test_permissions.py        Scope / ROLE_SCOPES
│   └── test_container_static.py   Containerfile 의 정적 검증
├── integration/
│   ├── test_app_factory.py        FastAPI create_app 부팅
│   ├── test_alembic_migrations.py 마이그레이션 round-trip
│   ├── test_db_session.py         session_scope commit/rollback
│   ├── test_cli_*.py              Typer CLI 의 CliRunner 기반
│   ├── test_routers_*.py          각 REST 라우터
│   ├── test_ui_pages.py           Jinja 렌더링 + CSRF
│   ├── test_security_headers.py   응답 헤더
│   ├── test_scheduler_jobs.py     APScheduler 의 잡 본체
│   ├── test_approval_flow.py      signup → approve → login
│   ├── test_repo_lifecycle.py     create → quota → push → reconcile
│   ├── test_spaces_registry.py    Spaces CRUD + 사이드카
│   ├── test_git_smart_http.py     실제 git 바이너리 round-trip
│   └── test_e2e_smoke.py          `make smoke` 가 실행
└── fixtures/                      정적 응답 / 인증서 / git 저장소
    ├── certs/
    ├── dns_responses/
    └── git_repos/
```

## 5. 실제 git round-trip (`test_git_smart_http`)

이 테스트는 컨테이너 없이도 실제 동작을 검증하는 핵심 통합 테스트입니다.

- `git` 바이너리로 임시 bare repo 와 클라이언트를 만듦
- `GitSmartService` 를 ASGI 앱으로 띄움
- `httpx.AsyncClient` 로 `/info/refs` 와 `git-receive-pack` 요청을 전송
- 응답 헤더 / 푸시 성공 후 `Revision` 행 / `UserUsage` 증가를 모두 검증

`make smoke` 는 이 파일만 별도로 실행하므로 컨테이너가 없는 개발 머신에서도
빠르게 통합 테스트를 돌릴 수 있습니다.

## 6. 컨테이너 동작 테스트 (테스트 환경에서만)

별도 머신에서 다음을 확인하세요.

```bash
# 1) 이미지 빌드
make build-stable

# 2) 데이터 디렉터리 준비
sudo mkdir -p /var/lib/outo-models
sudo chown -R 1000:1000 /var/lib/outo-models

# 3) 비대화형 setup (수동 DNS 모드 + skip-firewall)
sudo outo-models setup --non-interactive \
  --domain models.example.com \
  --acme-email admin@example.com \
  --dns-provider manual \
  --public-ipv4 127.0.0.1 \
  --admin-username admin \
  --admin-email admin@example.com \
  --admin-password 'changeme' \
  --skip-firewall --skip-dns --yes

# 4) start + status
sudo outo-models start
outo-models status

# 5) dry-run reset
outo-models reset

# 6) update
sudo outo-models update --image outo-models:stable
```

## 7. 새로운 코드 변경 시 체크리스트

AGENTS.md §6 절차와 일치합니다.

1. 변경 전에 관련 `docs/*.md` 를 읽는다.
2. `tests/unit/test_<module>.py` 또는 `tests/integration/test_<module>.py` 에
   테스트를 먼저 / 동시에 추가한다.
3. `make lint typecheck test` 통과를 확인한다.
4. 문서 불일치가 생기면 **문서를** 수정한다.
5. 사용자가 명시적으로 요청하기 전까지는 git commit / push 하지 않는다.

## 다음 단계

- [AGENTS.md §4](../AGENTS.md) — 개발 / 테스트 환경 분리 원칙
- [architecture.md](architecture.md) — 코드 모듈 지도
- [troubleshooting.md](troubleshooting.md) — 테스트 환경에서 자주 만나는 문제
