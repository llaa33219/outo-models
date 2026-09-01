# git 저장소 사용법

`outo-models` 의 저장소는 git 만으로 사용할 수 있습니다 — `git clone`,
`git push`, `git pull` 이 곧 사용자 인터페이스입니다. 본 문서는
[src/outo_models/git_smart](../src/outo_models/git_smart) 와
[src/outo_models/repos](../src/outo_models/repos) 가 실제로 어떻게 동작하는지를
운영자 / 사용자 관점에서 정리합니다.

## URL 형식

저장소 URL 은 Hugging Face 스타일을 따릅니다.

```
https://<도메인>/<소유자>/<이름>.git
```

예시:

```bash
git clone https://models.example.com/alice/ll-7b.git
git clone https://models.example.com/bob/wiki-en-dataset.git
git clone https://models.example.com/alice/demo-space.git
```

`.git` 접미사는 선택입니다 — 서버는 둘 다 받아서 동일한 bare repo 로
라우팅합니다 (`git_smart.service._parse_path` 에서 정규화).

## 인증: Basic Auth = username + PAT

서버는 HTTP Basic 인증을 받습니다. **비밀번호 칸에는 개인 액세스 토큰
(PAT) 을 넣어야 합니다** — 일반 로그인 비밀번호는 git endpoint 에서 받지
않습니다.

```bash
# 한 번만 자격 증명을 저장
git config --global credential.helper store
git clone https://alice:<PAT>@models.example.com/alice/ll-7b.git
# 또는 매번 프롬프트
git clone https://models.example.com/alice/ll-7b.git
# Username: alice
# Password: <PAT>
```

PAT 발급 절차:

1. 웹 UI 로그인 → 사용자 메뉴 → **Tokens**
2. **Create token** → 이름 / scopes (`read`, `write`) / 만료일 입력
3. 응답에 평문이 한 번만 표시됨 — 즉시 저장
4. 또는 API: `POST /api/auth/tokens` (`name`, `scopes`, `ttl_days`)

생성된 토큰은 PASETO v4 local 형식이며 90일 (기본) 후에 만료됩니다. 자세한
내용은 [security.md](security.md#personal-access-token-pat) 참고.

## 저장소 종류 (`kind`)

`Repo.kind` 는 셋 중 하나입니다 (SQL `model` / `dataset` / `space`).

| 종류 | 용도 | REST 엔드포인트 |
| --- | --- | --- |
| `model` | 모델 가중치 + 모델 카드 | `POST/GET/PATCH/DELETE /api/repos` |
| `dataset` | 데이터셋 파일 + README | `POST/GET/PATCH/DELETE /api/repos` |
| `space` | Spaces 메타데이터 (v1, 정적 페이지) | `POST/GET/PATCH/DELETE /api/spaces` |

같은 owner 가 같은 이름으로 `model` 과 `dataset` 을 동시에 가질 수 있습니다
(UNIQUE 제약이 `(owner_id, kind, name)` 이라 kind 가 다르면 충돌하지 않음).

생성 예시:

```bash
# 모델
curl -X POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"ll-7b","kind":"model","visibility":"private","description":"... "}' \
  https://models.example.com/api/repos

# 데이터셋
curl -X POST ... -d '{"name":"wiki-en","kind":"dataset", ...}' \
  https://models.example.com/api/repos

# Space
curl -X POST ... -d '{"name":"demo","sdk":"gradio", ...}' \
  https://models.example.com/api/spaces
```

`kind` 는 생성 후 변경할 수 없습니다 (`PATCH` 에는 visibility / description
만 노출). Spaces 의 `sdk` 도 v1 에서는 변경 불가 (실제 런타임이 무엇인지에
대한 약속이므로).

## 가시성 (`visibility`)

- `private` — owner 와 admin 만 읽기 / 쓰기 가능
- `public` — 익명 포함 누구나 `git clone` 가능

익명 클라이언트는 다음만 가능합니다.

- `public` 저장소의 `git clone` / `git pull` / `git fetch` (PULL 만)
- REST 의 `GET /api/repos`, `GET /{owner}/{name}` 페이지 (404 leak 방지)

private 저장소를 익명으로 `clone` 하면 WWW-Authenticate 챌린지가 나옵니다.
올바른 PAT 를 입력해도 owner 가 아니면 `403 Forbidden`. 자세한 매트릭스는
[security.md](security.md) 와 [architecture.md](architecture.md) 참고.

## 첫 push

```bash
cd my-model
git init
git remote add origin https://models.example.com/alice/my-model.git
git add .
git commit -m "initial"
git push -u origin main
```

`git push` 가 서버에서 거치는 단계:

1. URL → `(owner, name)` → DB 의 `Repo` 행 조회 (없으면 `404`)
2. `Authorization: Basic ...` → username / PAT 매칭 → `User` 획득
3. `authorize(user, repo, owner, PUSH)` — owner 본인이거나 admin 이어야 통과
4. `check_push_allowed(session, owner, Content-Length)` — 쿼터 초과 시 `413`
5. WSGI↔ASGI 어댑터 → dulwich 가 pack 처리 → 응답
6. 성공 (2xx) 시:
   - `REPO_LOCKS.acquire(owner, name)` 로 per-repo 직렬화
   - 새로 advance 한 `refs/heads/*` 각각에 대해 `Revision` 행 삽입
   - `Repo.size_bytes` 갱신
   - `UserUsage.used_bytes` += delta (음수면 0 클램프)
   - `AuditLog(action="repo.push", detail=...)` 기록

LFS 요청은 위 일반 푸시 파이프라인으로 들어오지 않고 별도 디스패치
([`git_smart/lfs.py`](../src/outo_models/git_smart/lfs.py)) 가 처리합니다 — 자세한
흐름은 [LFS 사용법](#lfs-사용법-v2) 참고.

## 쿼터 413

`check_push_allowed` 가 `used + incoming > max_bytes` 를 감지하면 즉시 413을
반환합니다. 응답 본문은 평문 한국어 메시지 (`QuotaExceededError`) 입니다.

```
HTTP/1.1 413 Request Entity Too Large
Content-Type: text/plain; charset=utf-8

quota exceeded: used=12582912000 + incoming=2147483648 > max=10737418240
```

해결 방법:

- 사용하지 않는 저장소를 삭제 (`DELETE /api/repos/<owner>/<name>` 또는 UI)
- 운영자에게 쿼터 상향 요청 (`outo-models admin quota set <name> 50GiB`)
- `quota_reconcile_job` 이 매시간 정확성을 다시 측정하므로, 디스크 회수
  후 다음 틱을 기다릴 필요는 없음 (push 가 다시 한 번 `add_usage` 로 보정)

## LFS 사용법 (v2)

v2 부터 `git lfs` 가 동작합니다 — 클라이언트는 변경 없이 `git lfs install`,
`git lfs track "*.bin"`, `git lfs push` 하면 됩니다. 백엔드는
`OUTO_LFS_BACKEND` 로 선택합니다 (`local` 기본, `s3`).

### 동작 개요

| 엔드포인트 | 메서드 | 처리 | 비고 |
| --- | --- | --- | --- |
| `/{owner}/{name}.git/info/lfs/objects/batch` | `POST` | `git_smart/lfs.py` `_handle_batch` | 업로드/다운로드 action URLs 반환. 인증 + 쿼터 + 사이즈 cap 검사 |
| `/{owner}/{name}.git/info/lfs/objects/{oid}` | `PUT` | `_handle_put` | `local` 백엔드만. 스트리밍 업로드, sha256 검증, `add_usage` |
| `/{owner}/{name}.git/info/lfs/objects/{oid}` | `GET` | `_handle_get` | `local` 백엔드만. 64 KiB 청크 스트리밍 |
| `/{owner}/{name}.git/info/lfs/locks*` | `*` | `lfs_not_supported` | **501** — 잠금은 v3 |

`local` 백엔드일 때 PUT/GET 은 **same-origin** 으로 처리되므로 `git-lfs` 가 원래
클론/푸시에 쓰던 Basic 자격 증명을 그대로 재사용합니다 (별도 헤더 없이도 동작).
`s3` 백엔드일 때는 batch 응답의 `actions.upload` / `actions.download` 가
**presigned URL** 이라 클라이언트가 그 URL 로 직접 S3 호환 엔드포인트에 요청합니다
— 이 경우 서버의 PUT/GET 핸들러는 호출되지 않고 (호출되더라도 `501` 로 거절)
S3 가 트래픽을 받습니다.

### 클라이언트 사용

```bash
# 1) 한 번만 LFS 설치 + 추적 패턴 등록
git lfs install
git lfs track "*.safetensors"
git lfs track "*.bin"
git add .gitattributes

# 2) 평소처럼 push — git-lfs 가 자동으로 batch API 를 호출합니다
git push -u origin main
# 3) 다른 머신에서 pull 할 때도 평소처럼
git clone https://models.example.com/alice/ll-7b.git
git lfs pull
```

Basic 인증은 일반 clone/push 와 동일합니다 — username + PAT. 자세한 자격 증명
방법은 [인증: Basic Auth = username + PAT](#인증-basic-auth--username--pat) 참고.

### 오류 코드

`/info/lfs/objects/batch` 는 거의 모든 오류를 **per-object** 로 표현합니다 — 한 객체가
실패해도 batch 전체가 실패하지 않습니다. 응답 예시는 다음과 같습니다.

```json
{
  "transfer": "basic",
  "objects": [
    { "oid": "aaaa…", "size": 1048576,
      "actions": { "upload": { "href": "…", "expires_in": 3600 } } },
    { "oid": "bbbb…", "size": 2147483648,
      "error": { "code": 413, "message": "object size 2147483648 exceeds per-object limit 5368709120" } },
    { "oid": "cccc…", "size": 5242880,
      "error": { "code": 413, "message": "quota exceeded: used=… + incoming=… > max=…" } }
  ]
}
```

batch 엔드포인트 자체가 반환할 수 있는 상태 코드:

| 코드 | 의미 | 트리거 |
| --- | --- | --- |
| `200` | 정상 — 클라이언트가 entries 를 순회하며 개별 결과를 확인 |
| `406 Not Acceptable` | `Accept` 헤더에 `application/vnd.git-lfs+json` 가 없음 |
| `415 Unsupported Media Type` | `Content-Type` 이 LFS 가 아님 |
| `413 Payload Too Large` | batch 본문이 1 MiB cap 초과, 또는 PUT 의 `Content-Length` 가 `OUTO_LFS_MAX_OBJECT_BYTES` 초과 |
| `422 Unprocessable Entity` | batch JSON 파싱 실패, oid 64자/16진 검증 실패, operation/`transfers` 값 이상 |
| `401 Unauthorized` | Basic 자격 증명 누락/무효 |
| `403 Forbidden` | 인증은 됐지만 권한 없음 (private 저장소 + non-owner) |
| `404 Not Found` | 저장소 없음, 또는 `GET /objects/{oid}` 의 객체 없음 |
| `500 Internal Server Error` | 설정 오류 (예: `OUTO_LFS_BACKEND=s3` 인데 OUTO_S3_ENDPOINT 가 비어 있음) |

locks 엔드포인트 (`/info/lfs/locks/*`) 는 v3 까지 항상:

```
HTTP/1.1 501 Not Implemented
Content-Type: application/json
Cache-Control: no-store

{"error": "Git LFS locks are not supported yet", "docs": "/docs/git-lfs"}
```

### 백엔드 설정 (`OUTO_LFS_BACKEND`)

기본은 `local` 입니다. `local` 은 `OUTO_DATA_DIR` 의 `lfs/<aa>/<bb>/<oid>` 로
샤딩 저장합니다 — 별도 설정이 필요 없습니다. **MinIO 같은 S3 호환 스토리지를
쓰려면** [`architecture.md`](architecture.md#lfs-request-flow) 의 S3 백엔드
설명을 참고해 다음을 채워 주세요.

```yaml
# /etc/outo-models/config.yaml (해당 키만 발췌)
lfs_backend: s3
s3_endpoint: http://minio.local:9000
s3_bucket: outo-lfs
s3_region: us-east-1
# OUTO_S3_ACCESS_KEY / OUTO_S3_SECRET_KEY 는 YAML 이 아니라 환경 변수로 주입
s3_prefix: lfs
s3_presign_ttl_seconds: 3600
```

전환 절차:

1. MinIO 에 `outo-lfs` 버킷을 생성하고 access key / secret key 를 발급
2. 컨테이너의 `outo-models` 볼륨이 아니라 별도 호스트의 MinIO 데몬이 `s3_endpoint`
   로 도달 가능해야 함 — 네트워크 구성은 호스트 측 책임
3. `config.yaml` 의 `lfs_backend` 를 `s3` 로 바꾸고 `restart`
4. 기존 local LFS 객체가 있다면 `mc cp --recursive` 로 MinIO 로 옮긴 뒤 oid 경로
   그대로 `<s3_prefix>/<aa>/<bb>/<oid>` 에 두면 호환됩니다

자세한 MinIO 세팅 절차는 [MinIO 공식 문서](https://min.io/docs/minio/linux/index.html) 와
[security.md §LFS auth model](security.md#lfs-auth-model) 참고.

## 동시성

- per-repo `asyncio.Lock` (`RepoLockRegistry.REPO_LOCKS`) 으로 같은 저장소의
  동시 push 가 직렬화됨 — dulwich 의 on-disk 상태와 DB `Revision` /
  `UserUsage` 사이의 일관성이 깨지지 않음
- 다른 저장소의 push 는 병렬로 진행
- `quota_reconcile_job` 이 매시간 모든 사용자에 대해 `disk_usage` 를 다시
  측정해 `UserUsage` 의 드리프트 보정

## 다음 단계

- [spaces.md](spaces.md) — Space 저장소 생성
- [admin.md](admin.md) — 쿼터 / 차단 운영
- [security.md](security.md) — 인증 메커니즘 상세
