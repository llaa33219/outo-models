# 아키텍처

이 페이지는 운영자가 시스템 동작을 머릿속에 그릴 수 있도록 돕습니다. 구현
세부 사항은 코드와 함께 진화하므로, 본 문서가 코드와 어긋날 때마다 PR 로
갱신합니다 (AGENTS.md §3).

## 모듈 지도

```
src/outo_models/
├── config.py, logging.py, exceptions.py      코어 인프라
├── utils/                                     경로, 슬러그, 시간, 해시
├── auth/                                      argon2, 세션, PASETO PAT, 권한, 레이트리밋
│   ├── approval.py                            가입 승인 상태 머신
│   ├── passwords.py                           argon2id 래퍼
│   ├── permissions.py                         scope / role
│   ├── rate_limit.py                          slowapi Limiter
│   ├── sessions.py                            itsdangerous 세션 쿠키
│   └── tokens.py                              PASETO v4 local + 지문
├── db/
│   ├── engine.py, session.py                  SQLAlchemy 비동기 엔진 / 세션
│   ├── models/                                ORM 모델
│   └── migrations/                            Alembic 마이그레이션
├── dns/
│   ├── base.py                                DNSProvider 추상 ABC + DnsRecord
│   ├── cloudflare.py                          Cloudflare 구현
│   ├── factory.py                             create_provider 디스패치
│   └── manual.py                              수동 모드 (안내 출력)
├── firewall/
│   ├── detect.py                              firewalld / ufw / nftables 감지
│   └── open_ports.py                          호스트 스크립트 호출
├── tls/
│   ├── caddy_manager.py                       Caddyfile 렌더링 + admin API
│   └── renewal.py                             인증서 헬스체크 + nudge
├── tasks/
│   ├── scheduler.py                           APScheduler 래퍼
│   └── jobs/                                  cert_renewal / quota_reconcile / audit_prune
├── repos/
│   ├── models.py                              RepoKind / Visibility 도메인 모델
│   ├── storage.py                             디스크 레이아웃 + per-repo asyncio.Lock
│   ├── create.py, delete.py                   bare repo 생성 / 삭제
│   ├── quota.py                               UserQuota / UserUsage + reconcile
│   └── reflog.py                              최근 커밋 조회
├── spaces/
│   ├── registry.py                            SDK 사이드카 + CRUD
│   └── runtime.py                             RuntimeState / Status (v1 stub)
├── git_smart/
│   ├── service.py                             GitSmartService (dulwich 어댑터)
│   ├── auth.py                                Basic auth + authorize 매트릭스
│   └── lfs.py                                 LFS 501 스텁
├── server/
│   ├── app.py                                 create_app (FastAPI 팩토리)
│   ├── middleware.py                          SecurityHeadersMiddleware
│   ├── deps.py                                get_db / get_current_user / require_admin
│   ├── errors.py                              예외 → JSON envelope
│   ├── routers/                               auth, users, repos, spaces, admin, webhooks, ui
│   └── templates/                             Jinja HTML 템플릿
├── cli/                                       outo-models Typer CLI
│   ├── setup/                                 대화형 마법사
│   ├── admin/                                 사용자 / 쿼터 / GPU 관리
│   ├── start.py, stop.py, restart.py, status.py  컨테이너 lifecycle
│   ├── update.py, reset.py                    update / reset
│   └── server.py                              컨테이너 내부 serve / migrate
└── cli_remote/                                AdminApiClient (원격 admin 모드)
```

`container/` 디렉터리는 호스트 측 글루입니다.

```
container/
├── caddy/Caddyfile.j2                         Jinja Caddyfile 템플릿
├── rootfs/                                    컨테이너 이미지에 복사되는 파일 트리
│   ├── etc/outo-models/config.example.yaml
│   └── usr/local/bin/outo-entrypoint.sh
├── scripts/
│   ├── firewall-open.sh                       호스트 방화벽 조작
│   ├── update.sh                              pull + migrate + restart
│   └── reset.sh                               컨테이너 / 볼륨 정리
├── examples/quadlet/                          podman systemd quadlet 예시
└── systemd/outo-models-host.service           부팅 시 방화벽 자동 개방 (opt-in)
```

## 데이터 레이아웃

기본 루트는 `OUTO_DATA_DIR` (기본 `/var/lib/outo-models`). 컨테이너 안에서도
같은 경로입니다 (Podman 볼륨 마운트).

```
/var/lib/outo-models/
├── db.sqlite3                      SQLite (또는 OUTO_DB_URL 의 Postgres)
├── repos/                          bare git 저장소
│   └── <owner>/
│       └── <name>.git/              dulwich 가 만든 bare repo
├── spaces/                         Spaces 사이드카
│   └── <owner>/
│       └── <name>.json              { "sdk": "static" | "gradio" | "streamlit" | "docker", ... }
├── certs/                          ACME 인증서 캐시 (Caddy 가 채움)
└── audit/                          감사 로그 (현재는 DB 안에 저장)
```

`utils.paths.ensure_dirs()` 가 5개 디렉터리 모두를 idempotent 하게 만듭니다.

## 데이터베이스 스키마

v1 의 단일 Alembic 마이그레이션 ([src/outo_models/db/migrations/versions/0001_initial.py](../src/outo_models/db/migrations/versions/0001_initial.py)) 이 다음 테이블을 만듭니다.

| 테이블 | 핵심 컬럼 | 비고 |
| --- | --- | --- |
| `users` | `id`, `username` (UNIQUE), `email` (UNIQUE), `password_hash`, `role` (`user`/`admin`), `status` (`pending`/`approved`/`denied`/`banned`), `display_name`, `approved_at`, `approved_by_id` | `status` 가 가입 흐름의 상태 머신 |
| `repos` | `id`, `owner_id` FK, `name`, `kind` (`model`/`dataset`/`space`), `visibility`, `description`, `default_branch`, `size_bytes`, `path` | UNIQUE `(owner_id, kind, name)` |
| `revisions` | `id`, `repo_id` FK, `commit_sha`, `branch`, `author_id` FK, `message`, `size_bytes` | push 후 git smart-HTTP 가 채움 |
| `personal_access_tokens` | `id`, `user_id` FK, `name`, `fingerprint_hash` (argon2id), `prefix`, `scopes` (JSON), `expires_at`, `last_used_at` | 평문 토큰은 저장하지 않음 |
| `approvals` | `id`, `user_id` FK UNIQUE, `decision`, `reason`, `decided_by_id` FK, `decided_at` | 가입 결정 추적 |
| `user_quotas` | `id`, `user_id` FK UNIQUE, `max_bytes` | 운영자가 설정 |
| `user_usages` | `id`, `user_id` FK UNIQUE, `used_bytes` | reconcile 가 채움 |
| `audit_logs` | `id`, `actor_id` FK, `action`, `target_type`, `target_id`, `detail`, `ip`, `created_at` | 모든 관리 동작 / push / signup 기록 |
| `web_settings` | `id`, `key` UNIQUE, `value`, `created_at`, `updated_at` | GPU 할당 등 자유 형식 키/값 |

## 요청 흐름

### 외부 클라이언트 → Caddy → uvicorn

```
브라우저 / git CLI
        │  HTTPS (80 → Caddy, 443 → Caddy)
        ▼
Caddy (in-container) :80/:443
        │  - ACME 발급/갱신 (HTTP-01 또는 DNS-01 cloudflare)
        │  - TLS 종료
        │  - reverse_proxy 127.0.0.1:8000
        ▼
uvicorn (127.0.0.1:8000) ← outo-models serve
        │  lifespan: run_migrations + TaskScheduler.start
        │
        ├── /api/*                       FastAPI 라우터 (auth/users/repos/spaces/admin/webhooks)
        │       │
        │       └── SecurityHeadersMiddleware (HSTS / CSP / X-Frame-Options ...)
        │       └── SlowAPIMiddleware (rate limit)
        │       └── get_current_user / require_admin deps
        │
        ├── /, /login, /signup, /admin/*  UI 라우터 (Jinja2 + CSRF double-submit)
        │
        └── /{owner}/{name}.git/...      GitSmartService (root mount)
                │
                ├── reject LFS early → 501 stub
                ├── resolve Repo + owner from DB
                ├── resolve_git_identity (Basic <b64(username:PAT)>)
                ├── authorize(user, repo, owner, action)
                ├── if PUSH: check_push_allowed → 413 on quota
                ├── _WsgiToAsgi → dulwich.web.HTTPGitApplication
                └── on PUSH success: per-repo lock + record Revision + AuditLog
```

### CLI 호출 흐름 (호스트)

```
outo-models <subcommand>
        │
        ▼
Typer app (cli/main.py) — OutoError → 한국어 1줄 + exit 1
        │
        ├── setup / update / start / stop / restart / status
        │       │
        │       └── setup → _collect (프롬프트) → _effect (config.yaml, DNS, firewall, DB, admin)
        │       └── update → container/scripts/update.sh
        │       └── start  → podman run (config.yaml 기반)
        │       └── stop/restart/status → podman 호출
        │
        └── admin → _commands → _local_db (SQL) | AdminApiClient (HTTP)
```

## 쿼터 모델

- `UserQuota.max_bytes` — 운영자가 설정 (기본 `OUTO_DEFAULT_QUOTA_BYTES`)
- `UserUsage.used_bytes` — push 후 즉시 증가 / 삭제 시 즉시 감소
- 매시간 `quota_reconcile_job` 이 모든 사용자에 대해 `disk_usage` 를 다시
  측정해 `UserUsage.used_bytes` 를 정정 (드리프트 보정)
- `check_push_allowed` 는 `used + incoming > max` 면 `QuotaExceededError` 로
  `413` 응답을 반환해 push 자체를 거부
- `Repo.size_bytes` 는 push 후 `disk_usage(repo_fs_path)` 결과로 갱신

자세한 코드 위치: [src/outo_models/repos/quota.py](../src/outo_models/repos/quota.py),
[src/outo_models/tasks/jobs/quota_reconcile.py](../src/outo_models/tasks/jobs/quota_reconcile.py).

## 스케줄러 잡

`TaskScheduler` (APScheduler 래퍼) 가 세 가지 잡을 등록합니다. 모두
`max_instances=1`, `coalesce=True`, `misfire_grace_time=3600` 으로 겹침
방지 / 지연 흡수.

| ID | 트리거 | 본체 | 무엇을 하나 |
| --- | --- | --- | --- |
| `cert_renewal` | 매일 00:00 UTC | `cert_renewal_job` | 도메인:443 TLS 핸드셰이크 → `CertHealth` → 비정상이고 Caddy 도 reachable 이면 Caddy reload 로 nudge |
| `quota_reconcile` | 매 1시간 | `quota_reconcile_job` | 모든 사용자에 대해 `disk_usage` 재측정 → `UserUsage` 정정 |
| `audit_prune` | 매일 02:00 UTC | `prune_audit_logs` | 90일 이전 `AuditLog` 행 삭제 (기본 보존 90일) |

세 잡 모두 절대 raise 하지 않습니다. 일시적 오류는 structlog warning 으로
남기고 다음 틱에서 재시도합니다.

## 이미지 플레이버

`Containerfile` 의 `IMAGE_FLAVOR` ARG 가 두 최종 타겟을 결정합니다.

- `outo-models:stable` — 운영용. `OUTO_ENV=production`. debugpy / ipython 없음.
- `outo-models:dev` — 개발용. `OUTO_ENV=development`. debugpy + ipython 포함.

엔트리포인트 (`/usr/local/bin/outo-entrypoint.sh`) 가 다음 가드를 강제합니다
(AGENTS.md §4).

> `IMAGE_FLAVOR=dev` + `OUTO_ENV=production` 조합은 거부하고 exit 1.

이외 동작은 양 플레이버가 동일합니다 (패키지 / 네트워크 정책 / 디스크
레이아웃 등 모두 같음).

### Quadlet 예시

[container/examples/quadlet/outo-models.container](../container/examples/quadlet/outo-models.container)
는 podman systemd quadlet 유닛 예시입니다. 운영 시에는 다음만 조정하면 됩니다.

- `Image=` — `outo-models:stable` (운영) 또는 `:dev` (테스트)
- `PublishPort=` — 80, 443 그대로 (loopback 매핑은 [troubleshooting.md](troubleshooting.md) 참고)
- `Volume=outo-models-data:/var/lib/outo-models` — 이름 변경 금지 (정적 테스트가 검사)
- `Environment=OUTO_ENV=production` 외에 시크릿은 `systemd-creds` 등 외부 저장소 사용

## 부팅 시 방화벽 자동 개방 (opt-in)

[container/systemd/outo-models-host.service](../container/systemd/outo-models-host.service) 는
컨테이너를 띄우기 전 호스트 측에서 80 / 443 을 자동으로 열어 주는 opt-in
헬퍼입니다.

```bash
sudo cp container/systemd/outo-models-host.service /etc/systemd/system/
sudo systemctl edit outo-models-host.service   # OUTO_FIREWALL_KIND 실제 값으로
sudo systemctl enable --now outo-models-host.service
```

기본은 `disabled` 상태입니다. 필요 없다면 그대로 두세요.

## 다음 단계

- [security.md](security.md) — 인증 / 토큰 / 레이트리밋 정책
- [git-repos.md](git-repos.md) — git 요청의 세부 처리
- [testing.md](testing.md) — 어떤 테스트가 어떤 흐름을 검증하는지
