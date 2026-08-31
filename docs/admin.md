# 관리자 가이드

이 문서는 운영자가 일상적으로 부딪히는 시나리오를 정리합니다. 모든 명령의
전체 플래그 레퍼런스는 [cli.md](cli.md) 를 보세요.

## 1. 가입 승인 / 거절

`outo-models setup` 직후 신규 가입은 `OUTO_REQUIRE_APPROVAL` (기본 `true`) 가
켜져 있으므로 `pending` 상태로 들어옵니다. 운영자는 다음 흐름으로 처리합니다.

```bash
# 1) 대기 목록 확인
sudo outo-models admin pending

# 2) 승인 또는 거절
sudo outo-models admin approve alice
sudo outo-models admin deny bob --reason "이메일 도메인 불일치"
```

승인 시 `User.status` 가 `pending` → `approved` 로, `Approval.decision` 도
같이 갱신되며 `AuditLog(action="user.approve")` 가 기록됩니다. 거절 사유는
`Approval.reason` (500자 이내) 에 저장됩니다.

가입 승인 제도를 끄려면 (셀프 서비스 가입) 설정 마법사에서
`--no-require-approval` 을 주거나, 운영 중에는 `/etc/outo-models/config.yaml` 의
`require_approval: false` 로 변경 후 컨테이너를 `outo-models restart` 하면
됩니다. 단, `signup` API 자체는 항상 열려 있으므로 승인제 운영을 권장합니다.

## 2. 사용자 차단 / 해제

```bash
sudo outo-models admin ban carol --reason "스팸 업로드"
sudo outo-models admin unban carol
```

- `ban` 은 `pending` / `approved` / `denied` 어느 상태에서나 가능
- 자기 자신을 차단하려고 하면 `ForbiddenError` 로 거부
- 다른 admin 을 차단하려는 시도도 `ForbiddenError` 로 거부
- 이미 차단된 사용자를 다시 차단하면 `ConflictError`

차단된 사용자는 인증 (세션 쿠키 / PAT) 모두 거부됩니다. `git_smart.auth.authorize` 가
`ForbiddenError("Account is not active")` 를 던지며 `git push` / `git pull` 모두
실패합니다.

## 3. 비밀번호 재설정

운영자가 임의로 비밀번호를 재설정해야 할 때 사용합니다. **1회만 stdout 으로
출력되므로** 즉시 캡처해 안전하게 전달하세요.

```bash
sudo outo-models admin reset-password alice
# [재설정] alice 의 새 비밀번호 (다시 출력되지 않습니다):
#   AbCdEf_GhIjKlMnOpQrS
```

원격 모드는 지원되지 않습니다. 비밀번호가 네트워크를 통과하지 않게 하기
위함입니다. 분실 시 SSH 로 서버에 직접 접속해 이 명령을 실행하세요.

## 4. 저장 용량 (쿼터)

기본 쿼터는 `OUTO_DEFAULT_QUOTA_BYTES` (기본 10 GiB). 신규 사용자마다 자동
부여되며 운영자가 변경할 수 있습니다.

```bash
# 조회 (KiB/MiB/GiB 자동 선택)
sudo outo-models admin quota show alice
# [쿼터] alice: max=10.00 GiB used=2.34 GiB

# 변경
sudo outo-models admin quota set alice 50GiB
# [쿼터] alice: max=50.00 GiB
```

크기는 사람이 읽기 좋은 단위를 받습니다 (`parse_human_bytes`):

- 이진 단위 (KiB, MiB, GiB, TiB, PiB) — 1024 진법
- 십진 단위 (KB, MB, GB, TB, PB) — 1000 진법
- 단위 없는 정수 (바이트로 해석)

`push` 시 `check_push_allowed` 가 `used + incoming > max` 면 `QuotaExceededError`
(상태 413) 를 던지므로 push 자체가 실패합니다. 자세한 동작은
[git-repos.md](git-repos.md#쿼터-413) 와 [architecture.md](architecture.md#쿼터-모델) 참고.

## 5. GPU 할당

사용자별로 GPU ID 문자열을 자유 형식으로 부여할 수 있습니다. v1 은 운영자가
스스로 정한 라벨을 그대로 저장할 뿐, 실제 kubelet / nvidia plugin 연동은 v2
로드맵입니다.

```bash
sudo outo-models admin gpu show alice
# [GPU] alice: gpu-0, gpu-1

sudo outo-models admin gpu assign alice gpu-0 gpu-2
sudo outo-models admin gpu clear alice
```

저장 위치는 `web_settings(key="gpu:<username>")` 의 JSON 배열입니다. 모든 변경은
`AuditLog(action="admin.gpu")` 가 기록합니다.

## 6. 원격 모드 (`--api-url` + `--token`)

원격 서버에 PAT 로 접속해 같은 명령을 실행할 수 있습니다. SSH 없이 운영할 때
유용합니다.

```bash
# 1) 서버에서 admin PAT 발급
#    UI: 설정 → 토큰 또는
#    API: POST /api/auth/tokens {"name":"ops", "scopes":["read","write"]}
TOKEN=outo.paseto.v4.local.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 2) 같은 LAN 의 다른 머신에서
outo-models admin list \
  --api-url https://models.example.com \
  --token "$TOKEN"

outo-models admin approve alice \
  --api-url https://models.example.com \
  --token "$TOKEN"
```

`--api-url` 과 `--token` 은 반드시 함께 지정해야 합니다. 둘 중 하나만 주면
`ConfigError(--api-url 과 --token 은 함께 사용해야 합니다)` 로 거부합니다.

원격 호출은 다음 순서로 동작합니다.

1. `outo_models.cli_remote.AdminApiClient` 가 `Authorization: Bearer <PAT>` 헤더로
   HTTPS 호출
2. 서버는 `/api/auth/tokens` 의 저장된 argon2id 지문과 일치 여부로 인증
3. `require_admin` 의존성이 `role == "admin"` 검증
4. `/api/admin/*` 핸들러가 실제 SQL 트랜잭션 수행, `AuditLog` 기록

원격 모드에서 사용할 수 **없는** 명령은 단 하나, `admin reset-password` 입니다
(평문 비밀번호가 네트워크를 통과하지 않게 하기 위함).

## 7. 감사 로그 조회

감사 로그는 두 경로로 조회할 수 있습니다.

- API: `GET /api/admin/audit?limit=100` (Bearer PAT, admin 권한)
- DB 직접 조회 (서버 호스트에서): `sqlite3 /var/lib/outo-models/db.sqlite3 \
  "SELECT created_at, actor_id, action, target_type, target_id FROM audit_logs \
  ORDER BY id DESC LIMIT 20;"`

`audit_prune` 잡이 매일 02:00 UTC 에 90일 이전 로그를 삭제합니다
(`tasks/jobs/audit_prune.py`). 보존 기간은 `prune_audit_logs` 의 기본값.

## 8. 가입 정책 변경 (`require_approval`)

운영 중 가입 정책은 다음 순서로 바꿀 수 있습니다.

```bash
# 컨테이너에서 직접 환경 변수 수정 후 재시작
sudo podman stop outo-models
sudo podman rm outo-models
sudo outo-models start   # 새 환경 변수가 자동 반영됨
# 또는
sudo outo-models update --image outo-models:stable
```

또는 `/etc/outo-models/config.yaml` 의 `require_approval` 을 수정해 다음 컨테이너
시작부터 적용합니다.

## 다음 단계

- [cli.md](cli.md) — 모든 명령의 정확한 플래그
- [security.md](security.md) — admin PAT 의 안전 정책
- [git-repos.md](git-repos.md) — 쿼터 / 차단이 git 동작에 미치는 영향
