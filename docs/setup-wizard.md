# 설정 마법사 (`outo-models setup`)

`outo-models setup` 은 신규 설치를 위한 대화형 / 비대화형 설정 도구입니다.
호스트에서 한 번 실행해 `/etc/outo-models/config.yaml` 을 만들고, DNS / 방화벽
/ DB / 관리자 계정을 한 번에 정리합니다. **마법사는 멱등**이라 운영 중에도
같은 인자로 다시 호출해 비밀번호를 회전하거나 설정을 보정할 수 있습니다.

본 문서는 [cli/setup/_collect.py](../src/outo_models/cli/setup/_collect.py) 와
[cli/setup/_effect.py](../src/outo_models/cli/setup/_effect.py) 가 실제로
수행하는 작업을 그대로 옮긴 것입니다. 코드와 다른 부분이 보이면 PR 로
문서를 갱신해 주세요.

## 한 줄 요약

```bash
sudo outo-models setup
```

대화형 모드에서는 아래 순서대로 질문이 나옵니다. 각 항목의 기본값은 `--yes`
플래그를 줬을 때 자동 채택되는 값입니다.

| # | 프롬프트 (한국어 그대로) | 검증 / 비고 |
| --- | --- | --- |
| 1 | `서버 도메인을 입력하세요 (예: models.example.com):` | `validate_domain` — 공백 / 슬래시 거부, 소문자 정규화 |
| 2 | `ACME (Let's Encrypt) 계정 이메일을 입력하세요:` | 만료 경고 수신 주소 |
| 3 | `DNS 제공자를 선택하세요 (cloudflare / manual):` | `cloudflare` 또는 `manual` 만 허용 |
| 4 | `Cloudflare API 토큰을 입력하세요 (Zone.DNS:Edit 권한):` | DNS 제공자가 `cloudflare` 일 때만 (비밀 입력) |
| 5 | `서버의 공개 IPv4 주소 (DNS A 레코드):` | `--skip-ip-detect` 가 아니면 `https://api.ipify.org` 로 자동 감지 후 기본값 |
| 6 | `관리자 계정 이름 (slug, 예: admin):` | `validate_slug` (소문자·숫자·`.` `_` `-`, 1–63자) |
| 7 | `관리자 계정 이메일을 입력하세요:` | `@` 포함 필수 |
| 8 | `관리자 비밀번호를 입력하세요 (8자 이상):` | 8자 이상 |
| 9 | `관리자 비밀번호를 다시 입력하세요:` | 첫 입력과 일치해야 함 |
| 10 | `외부에서 열 포트 (쉼표 구분, 기본 80,443):` | 각 포트 1–65535 |
| 11 | `신규 가입 시 관리자 승인을 요구하시겠습니까?` | 기본값 `true` (y/N) |

`--non-interactive` 모드에서는 위 모든 값을 플래그 또는 환경 변수로 받아야
합니다. 하나라도 빠지면 `ConfigError` 로 즉시 종료합니다.

## 플래그

| 플래그 | 의미 | 기본값 |
| --- | --- | --- |
| `--non-interactive` | 대화형 프롬프트 비활성화, 플래그 / 환경 변수만 사용 | `false` |
| `--domain <도메인>` | 서버 도메인 | (없음) |
| `--acme-email <이메일>` | ACME (Let's Encrypt) 계정 이메일 | (없음) |
| `--dns-provider <이름>` | `cloudflare` 또는 `manual` | (없음) |
| `--public-ipv4 <IPv4>` | DNS A 레코드로 만들 공개 IPv4 | (없음) |
| `--admin-username <slug>` | 관리자 계정 이름 | (없음) |
| `--admin-email <이메일>` | 관리자 계정 이메일 | (없음) |
| `--admin-password <비밀번호>` | 관리자 비밀번호 (8자 이상) | (없음) |
| `--skip-dns` | DNS 레코드 생성 단계 건너뜀 | `false` |
| `--skip-firewall` | 방화벽 포트 개방 단계 건너뜀 | `false` |
| `--skip-ip-detect` | 자동 IPv4 감지 건너뛰기 | `false` |
| `--yes` | 안전 단계에서 기본값 자동 수락 | `false` |
| `--ports <CSV>` | 쉼표 구분 포트 목록 | `80,443` |
| `--require-approval / --no-require-approval` | 신규 가입 승인 정책 | `true` |

## 자동 단계의 실제 동작

`_run_setup` 은 다음 순서로 실행됩니다. 모든 단계는 단일 트랜잭션이 아니며
순서대로 부분 완료될 수 있습니다 (멱등 재실행 가능).

### 1) 환경 변수 주입 (`apply_settings_env`)

수집한 값을 `OUTO_DOMAIN`, `OUTO_REQUIRE_APPROVAL`, `OUTO_ENV` 환경 변수로
밀어 넣고, `OUTO_SECRET_KEY` 가 없으면 `secrets.token_urlsafe(48)` 로 새로
만듭니다. `Settings` 의 LRU 캐시를 `cache_clear()` 해서 다음 호출에서 새
값을 읽도록 합니다.

### 2) `config.yaml` 작성 (`write_config`)

`OUTO_CONFIG` 환경 변수가 있으면 그 경로, 없으면 `/etc/outo-models/config.yaml` 에
다음 키들을 `yaml.safe_dump` 으로 저장합니다. **파일 모드는 `0o600`** 으로
설정되며, 실패 시에도 한국어 경고가 출력됩니다.

- `version` — 패키지 버전
- `domain`, `acme_email`, `public_ipv4`, `dns_provider`
- `image` — 기본 `outo-models:stable`
- `volume` — 기본 `outo-models-data`
- `ports` — 운영자가 입력한 목록
- `require_approval`
- `admin_username`, `admin_email`
- `cloudflare_api_token` (cloudflare 모드일 때만)
- `secret_key` (환경 변수에 있을 때만)

이 파일에는 비밀 키와 DNS API 토큰이 평문으로 들어 있으므로, 마법사는
"권한을 0o600 으로 유지하라" 는 한국어 경고를 stderr 로 출력합니다.

### 3) DNS A 레코드 (`ensure_dns_record`)

`--skip-dns` 가 아니면 다음을 수행합니다.

- `outo_models.dns.factory.create_provider` 로 `cloudflare` 또는 `manual` 구현체를 만듦
- `DnsRecord(name=<도메인>, type="A", value=<IPv4>, ttl=300)` 로 레코드 보장
- `manual` 모드면 `ManualProvider.instructions()` 가 한국어 안내를 stdout 으로
  출력하고, 운영자가 Enter 를 누를 때까지 대기 (`prompts.confirm(default=True)`)

자세한 동작은 [dns-providers.md](dns-providers.md) 를 보세요.

### 4) 방화벽 (`open_firewall_ports`)

`--skip-firewall` 가 아니면 다음을 수행합니다.

- `outo_models.firewall.detect.detect_firewall()` 로 백엔드 식별 (firewalld → ufw → nftables → none)
- `outo_models.firewall.open_ports.open_ports(ports=...)` 로 호스트 스크립트 호출
- 호스트 스크립트는 `bash firewall-open.sh <kind> <port...>` argv 로 실행
- 비루트면 `sudo -n` 자동 부착 (`firewall-open.sh` 가 `set -euo pipefail` 로 동작)

`sudo -n` 실패 시 `OutoError(code="firewall_permission")` 이 발생하고
마법사는 한국어 ConfigError 로 변환해 "root 로 다시 실행하거나
`/etc/sudoers.d/outo-models` 에 NOPASSWD 규칙을 추가하라" 는 안내를
출력합니다.

자세한 동작은 [troubleshooting.md](troubleshooting.md) 의 방화벽 섹션 참고.

### 5) 데이터 디렉터리 + DB + 관리자 계정 (`bootstrap_database`)

`utils.paths.ensure_dirs()` 로 5개 디렉터리 (`repos`, `spaces`, `certs`,
`audit`, 루트) 를 만든 뒤 다음을 수행합니다.

1. `outo_models.db.run_migrations(engine)` — `alembic upgrade head`
2. `outo_models.auth.passwords.hash_password(answers.admin_password)` — argon2id 해시
3. `session_scope()` 안에서 `User` 조회
   - 있으면: 이메일 / 해시 / `role=admin` / `status=approved` 갱신
   - 없으면: 새 `User(role="admin", status="approved", approved_at=now)` 추가
4. `dispose_engines()` 호출

마법사 이후 어떤 비밀번호도 화면에 다시 출력되지 않습니다. 분실 시
`outo-models admin reset-password <username>` 으로 재설정합니다.

### 6) Caddyfile 렌더링 (`render_caddyfile_setup`)

`outo_models.tls.caddy_manager.render_caddyfile` 으로 [container/caddy/Caddyfile.j2](../container/caddy/Caddyfile.j2) 를
렌더링해 `/etc/outo-models/Caddyfile` 에 저장합니다. `TlsConfig` 의
`dns_provider` 는 `cloudflare` 일 때만 활성화됩니다.

자세한 출력 형태는 [security.md](security.md#caddy) 의 "Caddy 와 ACME" 섹션
참고.

### 7) 한국어 다음 단계 안내

마지막으로 stdout 으로 다음을 출력합니다.

```
[완료] 설정이 저장되었습니다.
  - 설정 파일: /etc/outo-models/config.yaml
  - Caddyfile: /etc/outo-models/Caddyfile

다음 명령으로 서버를 시작하세요:
  outo-models start

비밀번호는 화면에 다시 출력되지 않습니다.
분실 시 admin reset-password 로 재설정하세요.
```

## 비대화형 예시

```bash
# Cloudflare 자동 모드
sudo OUTO_CLOUDFLARE_API_TOKEN=<token> \
  outo-models setup --non-interactive \
    --domain models.example.com \
    --acme-email admin@example.com \
    --dns-provider cloudflare \
    --public-ipv4 203.0.113.10 \
    --admin-username admin \
    --admin-email admin@example.com \
    --admin-password "$(openssl rand -base64 24)" \
    --yes
```

```bash
# 수동 DNS 모드 (방화벽만 자동, DNS 는 운영자가 호스트 측에서 직접)
sudo outo-models setup --non-interactive \
    --domain models.example.com \
    --acme-email admin@example.com \
    --dns-provider manual \
    --public-ipv4 203.0.113.10 \
    --admin-username admin \
    --admin-email admin@example.com \
    --admin-password "$(openssl rand -base64 24)" \
    --skip-dns --yes
```

## 다음 단계

- [install.md](install.md) — 첫 컨테이너 시작 절차
- [admin.md](admin.md) — 가입 승인 / 쿼터 / GPU 운영
- [troubleshooting.md](troubleshooting.md) — 방화벽 권한 / 포트 바인딩 오류
