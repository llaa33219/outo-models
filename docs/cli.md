# CLI 레퍼런스

`outo-models` 는 단일 Typer 콘솔 스크립트 하나로 모든 운영 작업을 처리합니다.
소스 위치는 [src/outo_models/cli/main.py](../src/outo_models/cli/main.py) 이며,
진입점은 `pyproject.toml` 의 `outo-models = "outo_models.cli.main:app"` 입니다.

> 모든 하위 명령은 한국어 help 문자열을 가지고 있습니다 (`Typer(help=...)`).
> `OutoError` 는 한국어 한 줄 + 종료 코드 1 로 렌더링되며 Python traceback 은
> 절대 출력되지 않습니다 (AGENTS.md §2.1).

## 최상위 옵션

```bash
outo-models --version
# outo-models 0.1.0
```

| 옵션 | 의미 |
| --- | --- |
| `--version` | 패키지 버전을 출력하고 종료 (즉시) |
| `-h`, `--help` | 도움말 출력 |

인수 없이 실행하면 도움말이 출력됩니다 (`no_args_is_help=True`).

## 명령 트리

```
outo-models
├── setup                최초 대화형 설정 마법사
│   └── run              마법사 본체 (--non-interactive 등)
├── server               컨테이너 내부에서 실행되는 명령
│   ├── serve            FastAPI 앱을 uvicorn 으로 부팅
│   └── migrate          alembic upgrade head (DB 마이그레이션)
├── start                컨테이너 시작 (호스트에서)
├── stop                 컨테이너 중지
├── restart              컨테이너 재시작
├── status               컨테이너 실행 상태 확인
├── update               이미지 갱신 + DB 마이그레이션 + 재시작
├── reset                컨테이너와 데이터 모두 삭제 (3회 확인 게이트)
└── admin                사용자 / 쿼터 / GPU 관리
    ├── list             사용자 목록
    ├── pending          승인 대기 사용자 목록
    ├── approve <name>   가입 승인
    ├── deny <name>      가입 거절
    ├── ban <name>       사용자 차단
    ├── unban <name>     차단 해제
    ├── quota
    │   ├── show <name>  저장 용량 표시
    │   └── set <name> <size>  저장 용량 설정
    ├── gpu
    │   ├── show <name>  GPU 할당 표시
    │   ├── assign <name> <ids...>  GPU 할당
    │   └── clear <name>  GPU 할당 제거
    └── reset-password <name>  새 비밀번호 생성 (1회 출력)
```

## setup

`setup_app` (Typer 서브앱) 의 유일한 명령은 `run` 입니다. 자세한 프롬프트와
자동 단계는 [setup-wizard.md](setup-wizard.md) 에서 다룹니다.

```bash
sudo outo-models setup                                # 대화형
sudo outo-models setup --non-interactive --yes ...     # 비대화형
```

| 플래그 | 의미 |
| --- | --- |
| `--non-interactive` | 프롬프트 비활성화 (필수 플래그 / env 사용) |
| `--domain <도메인>` | 서버 도메인 |
| `--acme-email <이메일>` | ACME 계정 이메일 |
| `--dns-provider <cloudflare\|manual>` | DNS 제공자 |
| `--public-ipv4 <IPv4>` | DNS A 레코드 IPv4 |
| `--admin-username <slug>` | 관리자 계정 이름 |
| `--admin-email <이메일>` | 관리자 계정 이메일 |
| `--admin-password <비밀번호>` | 관리자 비밀번호 (8자 이상) |
| `--skip-dns` | DNS 단계 건너뜀 |
| `--skip-firewall` | 방화벽 단계 건너뜀 |
| `--skip-ip-detect` | 자동 IPv4 감지 건너뜀 |
| `--yes` | 안전한 단계에서 기본값 자동 수락 |
| `--ports <CSV>` | 쉼표 구분 포트 (기본 `80,443`) |
| `--require-approval / --no-require-approval` | 가입 승인 정책 |

## server

컨테이너 **내부**에서 실행되는 명령입니다. 호스트에서 직접 호출하지 마세요.

### serve

```bash
outo-models serve [--host 127.0.0.1] [--port 8000]
```

| 플래그 | 의미 | 기본 |
| --- | --- | --- |
| `--host <addr>` | uvicorn 바인딩 호스트 (Caddy 가 reverse-proxy) | `127.0.0.1` |
| `--port <port>` | uvicorn 바인딩 포트 (1–65535) | `8000` |

`Containerfile` 의 `CMD ["serve"]` 가 그대로 호출하는 명령이며,
`/usr/local/bin/outo-entrypoint.sh` 가 한국어 배너 + dev/prod 검증 후
`exec outo-models "$@"` 로 실행합니다.

### migrate

```bash
outo-models migrate
```

설정된 DB URL 에 대해 `alembic upgrade head` 를 실행합니다. `update.sh` 가
throwaway 컨테이너에서 호출합니다. 성공 0, 실패 1 로 종료해 호스트 스크립트가
검사할 수 있게 합니다.

## start

```bash
sudo outo-models start
```

`/etc/outo-models/config.yaml` 의 `image`, `volume`, `ports` 키를 읽어 다음을
실행합니다.

```bash
podman run -d --name outo-models \
  -e OUTO_DATA_DIR=... -e OUTO_SECRET_KEY=... -e OUTO_DOMAIN=... \
  -e OUTO_REQUIRE_APPROVAL=true -e OUTO_DB_URL=... \
  -v outo-models-data:/var/lib/outo-models \
  --cap-add NET_BIND_SERVICE \
  -p 80:80 -p 443:443 \
  outo-models:stable
```

`podman` 이 PATH 에 없으면 "이 명령은 서버 호스트에서 실행되어야 합니다" 라는
한국어 메시지를 stderr 로 출력하고 exit 1 합니다.

## stop

```bash
sudo outo-models stop
```

`podman stop outo-models` 호출. 컨테이너가 없으면 멱등하게 0 으로 종료.
`podman` 부재 시 start 와 동일하게 한국어 안내 + exit 1.

## restart

```bash
sudo outo-models restart
```

`podman restart outo-models` 호출. 동작과 부재 시 동작은 `stop` 과 동일.

## status

```bash
outo-models status
```

`podman container exists outo-models` → `podman inspect ... .State.Running` 으로
실행 여부 확인. 한국어 한 줄 출력:

- `[상태] 실행 중: outo-models`
- `[상태] 중지됨: outo-models`
- `[상태] 컨테이너 없음: outo-models`
- `[정보] 이 호스트에는 podman이 설치되어 있지 않습니다 (개발 환경).`

`status` 만은 `podman` 부재 시에도 **0 으로 종료**합니다 (정보성 명령). 다른
명령 (`start` / `stop` / `restart`) 은 부재 시 1 로 종료합니다.

## update

```bash
sudo outo-models update [--image outo-models:stable]
```

`container/scripts/update.sh` 를 호출합니다. 스크립트는 다음을 차례로 실행합니다.

1. `podman pull <image>`
2. `podman run --rm -v outo-models-data:/var/lib/outo-models <image> outo-models migrate`
3. `podman restart outo-models` (컨테이너가 있을 때만)

스크립트의 exit code 가 그대로 CLI 의 exit code 가 됩니다. 0 이 아니면
`OutoError(code="update_failed")` 로 렌더링되어 종료 1.

`--image` 플래그로 이미지 태그를 덮어쓸 수 있습니다. 기본은 `outo-models:stable`.

## reset

```bash
outo-models reset                  # dry-run (기본)
outo-models reset --destroy        # 실제 삭제 (게이트 필요)
OUTO_DESTRUCTIVE=1 outo-models reset --destroy   # 실제 삭제
```

**3회 yes 게이트 (AGENTS.md §2.2)** 는 변경할 수 없습니다. 동작은 정확히
다음과 같습니다.

| 호출 | 결과 |
| --- | --- |
| `outo-models reset` | dry-run 요약만 출력하고 exit 0 |
| `outo-models reset --destroy` | `OUTO_DESTRUCTIVE=1` 없으면 거절 메시지 + exit 1 |
| `OUTO_DESTRUCTIVE=1 outo-models reset` | `OUTO_DESTRUCTIVE=1` 있어도 `--destroy` 가 없으면 dry-run |
| `OUTO_DESTRUCTIVE=1 outo-models reset --destroy` | 3회 `yes` 게이트 통과 시 실제 삭제 |

dry-run 출력 예시:

```
[dry-run] 다음 데이터가 삭제됩니다 (실제 삭제는 수행하지 않음):
  - 사용자 수: 12
  - 저장소 수: 47
  - 디스크 사용량: 18.42 GiB
  - 컨테이너: outo-models
  - 볼륨: outo-models-data

실제로 삭제하려면 --destroy 옵션과 OUTO_DESTRUCTIVE=1 환경변수를 함께 사용하세요.
```

게이트는 정확히 세 번의 `yes` 프롬프트이며, `input()` 으로 입력받습니다.

- 답이 정확히 `yes` 여야 통과 (대소문자, 공백, `y`, 빈 줄 모두 거부)
- 한 번이라도 다른 답을 입력하면 즉시 중단 + exit 1
- EOF (Ctrl-D) 도 안전하게 중단

세 번 모두 통과하면 다음을 실행합니다.

1. `container/scripts/reset.sh` 호출 (호스트 측 컨테이너 / 볼륨 정리)
2. `data_dir` 의 로컬 사본이 있으면 `shutil.rmtree`

성공 시 stdout:

```
[완료] outo-models 가 초기 설치 상태로 되돌아갔습니다.
다시 시작하려면 `outo-models setup` 을 실행해 주세요.
```

## admin

`admin_app` 의 명령은 로컬 DB 와 원격 모드 두 경로를 모두 지원합니다.

### 공통 옵션

모든 admin 하위 명령 (단, `reset-password` 제외) 은 다음 두 옵션을 받습니다.

| 플래그 | 의미 |
| --- | --- |
| `--api-url <URL>` | 원격 서버 URL (예: `https://models.example.com`) |
| `--token <PAT>` | 원격 서버의 admin PAT |

`--api-url` 또는 `--token` 중 하나만 지정하면 `ConfigError(--api-url 과 --token 은 함께 사용해야 합니다)` 로 거부합니다. 둘 다 지정하면 해당 서버의 `/api/admin/*` 엔드포인트로 동작을 위임하고, 출력은 로컬과 동일합니다.

### list

```bash
outo-models admin list [--status pending|approved|denied|banned]
outo-models admin list --api-url https://models.example.com --token <PAT>
```

사용자 테이블을 stdout 으로 출력합니다. 컬럼: `username`, `email`, `role`,
`status`, `id`.

### pending

```bash
outo-models admin pending
```

`admin list --status pending` 의 단축 명령입니다.

### approve

```bash
outo-models admin approve <username>
```

`pending` 상태 사용자를 `approved` 로 전이합니다. `AuditLog(action="user.approve")`
가 함께 기록됩니다. `username` 이 존재하지 않거나 이미 approved/denied 면
`ConflictError` / `NotFoundError`.

### deny

```bash
outo-models admin deny <username> [--reason <text>]
```

가입을 거절하고 `Approval.reason` 에 사유를 저장합니다. `AuditLog(action="user.deny")`
기록. 사유는 500자 이내.

### ban

```bash
outo-models admin ban <username> [--reason <text>]
```

`pending` / `approved` / `denied` 사용자를 `banned` 로 전이합니다. 안전
규칙: 자기 자신 차단 금지, 다른 admin 차단 금지 (`ForbiddenError`). 이미 차단된
사용자는 `ConflictError`.

### unban

```bash
outo-models admin unban <username>
```

`banned` 사용자를 다시 `approved` 로 돌립니다. `Approval` 행의 이력은
유지됩니다 (감사 추적).

### quota show

```bash
outo-models admin quota show <username>
```

```
[쿼터] alice: max=10.00 GiB used=2.34 GiB
```

`max_bytes` / `used_bytes` 를 사람이 읽기 좋은 단위로 출력합니다. 내부적으로
`repos.quota.ensure_quota_rows` 로 행이 없으면 자동 생성합니다.

### quota set

```bash
outo-models admin quota set <username> <size>
```

`<size>` 는 사람이 읽기 좋은 단위 문자열을 받습니다. `parse_human_bytes` 가
다음 형식을 모두 지원합니다.

| 형식 | 의미 |
| --- | --- |
| `10GiB`, `10gib`, `10GIB` | 2^30 × 10 |
| `500MiB` | 2^20 × 500 |
| `100KB` | 10^3 × 100 |
| `10737418240` | 단위 없는 정수 (바이트) |

잘못된 입력은 `ValidationFailedError` (한국어 메시지). 적용 시
`AuditLog(action="admin.quota")` 기록.

### gpu show

```bash
outo-models admin gpu show <username>
```

```
[GPU] alice: gpu-0, gpu-1
# 또는 할당이 없으면:
[GPU] alice: 할당 없음
```

GPU ID 는 `web_settings(key="gpu:<username>")` 에 JSON 리스트로 저장됩니다.

### gpu assign

```bash
outo-models admin gpu assign <username> gpu-0 gpu-1 gpu-2
```

기존 할당을 **덮어씁니다**. 공백으로 구분된 ID 목록을 받습니다. `AuditLog(action="admin.gpu")` 기록.

### gpu clear

```bash
outo-models admin gpu clear <username>
```

할당을 완전히 제거합니다. `web_settings` 행이 없으면 멱등하게 no-op.

### reset-password

```bash
outo-models admin reset-password <username>
```

**로컬 전용** 명령입니다. `--api-url` / `--token` 옵션이 없습니다 (원격으로
비밀번호를 재설정하면 평문이 네트워크를 통과하게 됩니다). 내부적으로
`secrets.token_urlsafe(18)` 로 새 비밀번호를 생성해 argon2id 해시로 저장하고,
**1회만 stdout 으로 출력**합니다. 운영자는 출력물을 즉시 캡처해야 합니다.

```
[재설정] alice 의 새 비밀번호 (다시 출력되지 않습니다):
  AbCdEf_GhIjKlMnOpQrS
```

`AuditLog(action="admin.reset_password")` 가 함께 기록됩니다.

## 환경 변수

모든 `OUTO_*` 환경 변수는 Pydantic Settings 가 `OUTO_` 접두사를 떼고 매핑합니다.

| 변수 | 대응 Settings 필드 | 기본값 | 의미 |
| --- | --- | --- | --- |
| `OUTO_DATA_DIR` | `data_dir` | `/var/lib/outo-models` | DB, git 저장소, LFS, 인증서 캐시의 루트 |
| `OUTO_DOMAIN` | `domain` | `localhost` | 공개 도메인 (loopback 이면 http, 그 외 https) |
| `OUTO_DB_URL` | `db_url` | `null` (→ `sqlite+aiosqlite:///${OUTO_DATA_DIR}/db.sqlite3`) | SQLAlchemy URL |
| `OUTO_SECRET_KEY` | `secret_key` | `""` | 세션 / 토큰 서명 키 (production 에서 32자 이상) |
| `OUTO_ENV` | `env` | `development` | `development` 또는 `production` |
| `OUTO_REQUIRE_APPROVAL` | `require_approval` | `true` | 가입 시 관리자 승인 필요 여부 |
| `OUTO_DEFAULT_QUOTA_BYTES` | `default_quota_bytes` | `10737418240` (10 GiB) | 신규 사용자 기본 쿼터 |
| `OUTO_LFS_BACKEND` | `lfs_backend` | `local` | LFS 백엔드 (`local` / `s3`) |
| `OUTO_LFS_MAX_OBJECT_BYTES` | `lfs_max_object_bytes` | `5368709120` (5 GiB) | LFS 단일 객체 최대 크기 |
| `OUTO_S3_ENDPOINT` | `s3_endpoint` | `""` | S3 호환 endpoint URL |
| `OUTO_S3_BUCKET` | `s3_bucket` | `""` | S3 버킷 이름 |
| `OUTO_S3_REGION` | `s3_region` | `us-east-1` | S3 region |
| `OUTO_S3_ACCESS_KEY` | `s3_access_key` | `""` | S3 access key id |
| `OUTO_S3_SECRET_KEY` | `s3_secret_key` | `""` | S3 secret access key |
| `OUTO_S3_PREFIX` | `s3_prefix` | `lfs` | 버킷 안 객체 키 접두사 |
| `OUTO_S3_PRESIGN_TTL_SECONDS` | `s3_presign_ttl_seconds` | `3600` | presigned URL 유효 시간 |
| `OUTO_SPACES_RUNTIME_ENABLED` | `spaces_runtime_enabled` | `false` | Spaces 컨테이너 런타임 on/off |
| `OUTO_PODMAN_SOCKET` | `podman_socket` | `/run/podman/podman.sock` | Podman REST API Unix 소켓 |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` | `spaces_runtime_port_range_start` | `20000` | Space 컨테이너 호스트 포트 시작 |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_END` | `spaces_runtime_port_range_end` | `21000` | Space 컨테이너 호스트 포트 끝 |

그 외 운영 보조 환경 변수:

| 변수 | 의미 | 사용처 |
| --- | --- | --- |
| `OUTO_CONFIG` | YAML 설정 파일 경로 | `setup`, `start` 가 사용 (기본 `/etc/outo-models/config.yaml`) |
| `OUTO_DESTRUCTIVE` | `reset --destroy` 의 안전 게이트 | `1` 일 때만 게이트 통과 |
| `OUTO_CLOUDFLARE_API_TOKEN` | Cloudflare 모드에서 DNS 레코드 생성 | setup wizard (--admin-password 와 동등) |
| `OUTO_FIREWALL_SCRIPT` | 방화벽 호스트 스크립트 경로 오버라이드 | `firewall.open_ports` (기본 `container/scripts/firewall-open.sh`) |
| `OUTO_CADDYFILE_TEMPLATE` | Caddyfile 템플릿 경로 오버라이드 | `tls.caddy_manager` (기본 `container/caddy/Caddyfile.j2`) |
| `OUTO_UPDATE_SCRIPT` | update.sh 경로 오버라이드 | `cli.container_script` |
| `OUTO_RESET_SCRIPT` | reset.sh 경로 오버라이드 | `cli.container_script` |
| `OUTO_CADDY_ADMIN_URL` | Caddy 관리 API 베이스 URL | 서버 lifespan 의 cert health check (기본 `http://localhost:2019`) |
| `CLOUDFLARE_API_TOKEN` | Caddy DNS-01 챌린지에 사용 | Caddyfile `tls { dns cloudflare {env.CLOUDFLARE_API_TOKEN} }` |

## 종료 코드

- `0` — 성공
- `1` — `OutoError` (한국어 한 줄 출력) 또는 호스트 스크립트 실패
- 다른 코드 — 호스트 스크립트가 명시적으로 반환한 값 (예: `update.sh` 의
  alembic 실패)

## 다음 단계

- [admin.md](admin.md) — admin 명령의 운영 시나리오
- [setup-wizard.md](setup-wizard.md) — setup 의 프롬프트 상세
- [security.md](security.md) — 안전 게이트 / 토큰 정책
