# 보안

outo-models 의 보안 정책은 AGENTS.md §2 의 "보안 타협 금지" 원칙을 코드로
구현한 것입니다. 본 페이지는 운영자가 어떤 보호가 자동 적용되는지, 어떤
설정을 손대지 말아야 하는지를 한 곳에서 볼 수 있도록 정리합니다.

## 1. 비밀번호

저장: [src/outo_models/auth/passwords.py](../src/outo_models/auth/passwords.py).

- 알고리즘: **argon2id** (`argon2-cffi` 래퍼)
- 파라미터: `time_cost=3`, `memory_cost=64 MiB`, `parallelism=1`
  (OWASP "Password Storage Cheat Sheet" 권장치를 따름)
- 저장 형식: PHC 문자열 (`$argon2id$...`) — 알고리즘 / 파라미터 / 솔트가
  모두 인코딩되어 들어가므로 파라미터 업그레이드 시 호환성 보장
- 모든 호출에 새로운 랜덤 솔트가 생성됨 — 같은 비밀번호도 매번 다른 해시
- 검증 실패 (`VerificationError`, `InvalidHashError`) 는 절대 raise 하지 않고
  항상 `False` 반환 (사용자 열거 방지)
- `needs_rehash` 가 True 면 로그인 성공 후 새 해시로 자동 회전

비밀번호 정책:

- 가입 폼: 8자 이상 (Pydantic `min_length=8`)
- 관리자 비밀번호: 설정 마법사에서 8자 이상 검증, 동일 값 재입력

운영자가 분실 비밀번호를 복구할 때는 `outo-models admin reset-password <name>` 으로
새 비밀번호를 발급합니다. 평문은 1회만 stdout 으로 출력됩니다.

## 2. Personal Access Token (PAT)

저장: [src/outo_models/auth/tokens.py](../src/outo_models/auth/tokens.py).

- 토큰 포맷: **PASETO v4 local** (암호화 + 인증)
- 키 유도: `Settings.secret_key` → `sha256(secret)` → 32 바이트 PASETO 키
- 토큰 평문은 **절대 DB 에 저장하지 않음**
- DB 에는 `fingerprint_hash` (argon2id, `utils.hashing.hash_secret`) 와
  `prefix` (앞 8자) 만 저장
- 만료: 기본 `DEFAULT_TOKEN_TTL_SECONDS = 7_776_000` (90일)
- 발급 시 응답에 토큰 평문이 포함되며, **이 후로는 어떤 경로로도 다시 얻을
  수 없음**

검증 흐름:

1. `Authorization: Basic <b64(username:token)>` (git) 또는 `Bearer <token>` (API)
2. `match_fingerprint(pat.fingerprint_hash, token)` 으로 후보 PAT 행마다 검증
3. 매칭 시 `last_used_at` 갱신
4. 사용자가 banned / pending 이면 인증 결과를 `None` 으로 강제 변환

생성 / 폐기 / 나열 엔드포인트는 [cli.md](cli.md) 의 `POST/GET/DELETE
/api/auth/tokens` 와 일치합니다.

## 3. 세션 쿠키

저장: [src/outo_models/auth/sessions.py](../src/outo_models/auth/sessions.py).

- 라이브러리: `itsdangerous.URLSafeTimedSerializer`
- 쿠키 이름: `outo_session` (변경 금지 — 클라이언트 호환성 깨짐)
- 솔트: `outo-models.session.v1` (다른 용도로의 토큰 재사용 방지)
- 페이로드: `{ "user_id": <int>, "nonce": <secrets.token_urlsafe(16)> }`
- 만료: 7일 (`_SESSION_MAX_AGE_SECONDS`)
- **로그인마다 새 토큰 발급 (rotation)** — 세션 고정 공격 방어

`cookie_kwargs(secure)` 가 모든 쿠키 속성을 한 곳에서 정의합니다.

- `HttpOnly=True` — JS 접근 차단 (XSS 완화)
- `SameSite="Lax"` — 최상위 GET 내비게이션은 허용 (OIDC 스타일 흐름 호환)
- `Path="/"` — 모든 경로에서 사용 가능
- `Secure=True` (production) / `False` (development) — `Settings.env` 에서 결정

HSTS: `domain` 이 loopback 가 아니면 응답에 `strict-transport-security:
max-age=31536000; includeSubDomains` 가 자동 추가됩니다.

## 4. CSRF

저장: [src/outo_models/server/routers/_ui_helpers.py](../src/outo_models/server/routers/_ui_helpers.py).

UI 폼은 더블-submit 쿠키 방식으로 보호합니다.

1. `GET /signup`, `GET /login` 이 `_csrf` 쿠키를 발급
2. 같은 값이 `<input name="_csrf">` 로 폼에 렌더됨
3. `POST /signup`, `POST /login` 이 쿠키와 폼 값을 `secrets.compare_digest` 로 비교
4. 불일치 / 누락 시 403

CSRF 토큰도 itsdangerous 로 서명되며 솔트는 `outo-models.csrf.v1` (세션
쿠키와 분리). API 엔드포인트 (`/api/*`) 는 CSRF 대상이 아닙니다 — 브라우저
자동 쿠키 전송이 없으므로 토큰을 명시적으로 보내야 하는 인증 헤더 /
Basic 인증으로 보호됩니다.

## 5. 레이트 리밋

저장: [src/outo_models/auth/rate_limit.py](../src/outo_models/auth/rate_limit.py).

| 상수 | 값 | 적용 엔드포인트 |
| --- | --- | --- |
| `LOGIN_LIMIT` | `5/minute` | `POST /api/auth/login` |
| `SIGNUP_LIMIT` | `3/minute` | `POST /api/auth/signup` |
| `GIT_PUSH_LIMIT` | `30/minute` | git receive-pack (현재는 정의만, 적용은 v2) |
| `GIT_PULL_LIMIT` | `120/minute` | git upload-pack (현재는 정의만, 적용은 v2) |
| `API_LIMIT` | `240/minute` | 기본 REST API |

키 함수:

- `key_by_ip` — IP 주소 단위 버킷 (로그인 / 회원가입)
- `key_by_user_or_ip` — 인증된 사용자는 `user:<id>`, 아니면 IP — NAT 뒤의
  정당한 사용자가 서로 격리되지 않도록

제한 초과 시 slowapi 의 `RateLimitExceeded` 가 JSON 429 응답을 반환합니다.

## 6. 보안 헤더

저장: [src/outo_models/server/middleware.py](../src/outo_models/server/middleware.py).

모든 응답 (git smart-HTTP 스트림 포함) 에 다음 헤더가 자동 추가됩니다.

| 헤더 | 값 |
| --- | --- |
| `X-Content-Type-Options` | `nosniff` |
| `X-Frame-Options` | `DENY` |
| `Referrer-Policy` | `strict-origin-when-cross-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` |
| `Content-Security-Policy` | `default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'` |
| `Strict-Transport-Security` | `max-age=31536000; includeSubDomains` (loopback 가 아닐 때만) |

CSP 는 번들된 Jinja 템플릿이 인라인 `<style>` 블록을 사용하기 때문에
`style-src` 만 `'unsafe-inline'` 을 허용합니다. `script-src` 는 `'self'`
그대로 — 어떤 inline script 도 출하되지 않습니다.

## 7. 감사 로그

저장 위치: `audit_logs` 테이블 (DB).

- `actor_id` FK — 어떤 사용자가 (`None` 이면 가입 같은 시스템 액션)
- `action` — `user.signup`, `user.approve`, `user.deny`, `user.ban`, `user.unban`,
  `repo.create`, `repo.push`, `admin.quota`, `admin.gpu`, `admin.reset_password`
- `target_type`, `target_id` — 대상의 종류와 PK
- `detail` — JSON 문자열 (예: quota 의 old/new, push 의 branch advances)
- `ip` — 요청자 IP (라우터에서 채움; 현재 CLI 경로는 미기록)
- `created_at` — UTC 타임스탬프

`audit_prune` 잡이 매일 02:00 UTC 에 90일 이전 행을 삭제합니다. 보존 기간
변경은 `tasks/jobs/audit_prune.py` 의 `_DEFAULT_RETENTION_DAYS` 또는 직접
`prune_audit_logs(retention_days=N)` 호출로 가능합니다.

## 8. Caddy 와 ACME

저장: [src/outo_models/tls/caddy_manager.py](../src/outo_models/tls/caddy_manager.py),
[container/caddy/Caddyfile.j2](../container/caddy/Caddyfile.j2).

- Caddy 가 컨테이너 내부에서 80 / 443 을 소유하고 ACME 발급 / 갱신을 담당
- HTTP-01: 도메인이 일반 도메인이고 외부에서 80 으로 ACME 가 도달 가능할 때
- DNS-01: Cloudflare 모드일 때 `tls { dns cloudflare {env.CLOUDFLARE_API_TOKEN} }`
- `OUTO_TLS_STAGING=true` (또는 동등 플래그) 로 Let's Encrypt 스테이징 CA 사용 가능
  (도메인 오타로 인한 rate-limit 손실 방지)

Cloudflare 토큰은 Caddyfile 본문에 절대 들어가지 않고 `{env.CLOUDFLARE_API_TOKEN}`
치환으로 Caddy 프로세스의 환경 변수에서 읽습니다. 자세한 내용은 [dns-providers.md](dns-providers.md)
와 [security.md § 시크릿 위생](#시크릿-위생) 참고.

## 9. 시크릿 위생

다음 원칙을 코드 전반에 강제합니다 (AGENTS.md §2.1).

- 비밀번호 / 토큰 / DNS 토큰 / 시크릿 키는 **로그에 절대 찍지 않음**
- `__repr__` 이 시크릿을 노출하지 않음 (예: `CloudflareProvider.__repr__` 은
  zone 만 표시)
- 예외 메시지에 토큰이 포함되지 않음 (Cloudflare 응답은 `re.sub(r"[A-Za-z0-9_-]{32,}", "***", ...)` 으로 마스킹)
- `ConfigError` 로 노출되는 메시지는 운영자가 바로 진단할 수 있는 형태로만
  구성되며, 자격 증명은 포함하지 않음

`/etc/outo-models/config.yaml` 은 **모드 `0o600`** 으로 저장되며, 마법사가
한국어 경고를 출력합니다.

## 10. 컨테이너 비특권 실행

[Containerfile](../Containerfile) 에 따라 컨테이너는 uid/gid 1000 (`app`) 으로
실행됩니다.

- 방화벽 / DNS / 인증서 갱신 같은 호스트 권한이 필요한 작업은 컨테이너
  내부에서 직접 하지 않고 **호스트 측 스크립트** (`container/scripts/*.sh`) 가 처리
- Caddy 가 비특권으로 80 / 443 을 바인딩하려면 `--cap-add NET_BIND_SERVICE`
  가 필요 (start 명령이 자동 부착)
- 루트에서 비특권으로 갈 때는 pip install 같은 일시 작업 후 즉시 비특권으로
  복귀

## 11. 호스트 측 방화벽 경계

`outo-models` 컨테이너 자체는 호스트 방화벽을 직접 건드리지 않습니다.
다음 책임 분담이 명확합니다.

- 컨테이너 내부 CLI: `outo_models.firewall.open_ports` 가 argv 빌드 후
  `bash container/scripts/firewall-open.sh <kind> <port...>` 실행
- 호스트 측 스크립트: `firewall-cmd` / `ufw` / `nft` 직접 호출 (set -euo pipefail)
- 비루트 호출 시 `sudo -n` 자동 부착
- `sudo -n` 실패 시 `OutoError(code="firewall_permission")` 으로 명확한 메시지

자세한 흐름은 [architecture.md](architecture.md) 와 [troubleshooting.md](troubleshooting.md)
의 방화벽 섹션 참고.

## 12. `reset` 안전장치

[src/outo_models/cli/reset.py](../src/outo_models/cli/reset.py) 는 AGENTS.md §2.2
를 코드로 강제합니다.

- `--destroy` 없으면 **항상 dry-run** (삭제 안 함)
- `OUTO_DESTRUCTIVE=1` 없으면 `--destroy` 거절
- 둘 다 있을 때만 3회 `yes` 프롬프트
- 답은 정확히 `yes` (대소문자, 공백, `y`, 빈 줄 모두 거부)
- 한 번이라도 틀리면 즉시 중단 + exit 1
- EOF 도 안전하게 중단

**이 안전장치를 약화시키는 PR 은 거부됩니다.** 우회 경로도 없습니다.

## 다음 단계

- [admin.md](admin.md) — admin PAT 발급 / 폐기 운영
- [git-repos.md](git-repos.md) — git clone/push 시 자격 증명 흐름
- [troubleshooting.md](troubleshooting.md) — 시크릿 / 인증 문제 디버깅
