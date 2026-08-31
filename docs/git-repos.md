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

1. LFS 요청이면 즉시 `501` + JSON 안내 (`/docs/git-repos` 링크)
2. URL → `(owner, name)` → DB 의 `Repo` 행 조회 (없으면 `404`)
3. `Authorization: Basic ...` → username / PAT 매칭 → `User` 획득
4. `authorize(user, repo, owner, PUSH)` — owner 본인이거나 admin 이어야 통과
5. `check_push_allowed(session, owner, Content-Length)` — 쿼터 초과 시 `413`
6. WSGI↔ASGI 어댑터 → dulwich 가 pack 처리 → 응답
7. 성공 (2xx) 시:
   - `REPO_LOCKS.acquire(owner, name)` 로 per-repo 직렬화
   - 새로 advance 한 `refs/heads/*` 각각에 대해 `Revision` 행 삽입
   - `Repo.size_bytes` 갱신
   - `UserUsage.used_bytes` += delta (음수면 0 클램프)
   - `AuditLog(action="repo.push", detail=...)` 기록

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

## LFS 501 (로드맵)

`git-lfs` 클라이언트가 보내는 `*.git/info/lfs/objects` 요청은
`lfs_not_supported` 핸들러가 가로채서 즉시 501 을 반환합니다.

```
HTTP/1.1 501 Not Implemented
Content-Type: application/json
Cache-Control: no-store

{"error": "Git LFS is not supported yet", "docs": "/docs/git-repos"}
```

v1 의 의도된 동작입니다. LFS 객체 저장소 + 청크 단위 검증은 v2 로드맵. 그
전까지는 다음 우회 방법을 권장합니다.

- 모델 카드는 LFS 없이 직접 커밋
- 대용량 가중치는 `git-annex`, 외부 호스팅 링크, 또는 별도 데이터셋 repo 로 분할
- 진행 상황은 [spaces.md](spaces.md) 의 "v2 런타임" 섹션 참고

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
