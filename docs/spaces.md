# Spaces

Spaces 는 Hugging Face 의 `Spaces` 처럼 인터랙티브 데모를 호스팅하기 위한
저장소 종류입니다. v1 (현재 버전) 의 범위는 **메타데이터 + 정적 페이지**로
한정되어 있고, 컨테이너 런타임으로 코드를 실행하는 기능은 **v2 로드맵**입니다.

본 문서는 [src/outo_models/spaces](../src/outo_models/spaces) 의 실제 동작을
그대로 옮깁니다.

## v1 범위

- **저장소**: `Repo(kind="space")` — 일반 git 저장소와 동일한 인프라 사용
- **메타데이터**: `<data_dir>/spaces/<owner>/<name>.json` 사이드카 파일
- **REST**: `/api/spaces/*` (생성 / 목록 / 상세 / 수정 / 삭제)
- **UI**: `/<owner>/<name>` 페이지에 메타데이터 + clone URL 표시
- **런타임**: **없음** — 모든 Space 는 `runtime.state == "preview_unavailable"`

지원되는 SDK 목록 (`SUPPORTED_SDKS`):

| SDK | 의미 (v1) |
| --- | --- |
| `static` | 정적 페이지 / 데모 없는 README. **기본값** |
| `gradio` | 메타데이터로 "Gradio 앱" 라벨만 표시. 실행은 v2 |
| `streamlit` | 메타데이터로 "Streamlit 앱" 라벨만 표시. 실행은 v2 |
| `docker` | 메타데이터로 "Docker 컨테이너" 라벨만 표시. 실행은 v2 |

`SUPPORTED_SDKS` 에 없는 값을 `POST /api/spaces` 의 `sdk` 로 보내면 404 가
아닌 404 `unsupported sdk: '<x>'` 메시지로 거절됩니다 (코드는 `not_found`).

## 생성

```bash
curl -X POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"demo","sdk":"gradio","visibility":"public","description":"..."}' \
  https://models.example.com/api/spaces
```

응답:

```json
{
  "id": 42,
  "name": "demo",
  "sdk": "gradio",
  "visibility": "public",
  "description": "...",
  "owner": "alice",
  "clone_url": "https://models.example.com/alice/demo.git",
  "created_at": "2026-08-31T00:00:00+00:00"
}
```

내부적으로 다음 순서로 일어납니다.

1. `sdk` 가 `SUPPORTED_SDKS` 에 있는지 검증 (실패 시 404 + 한국어 메시지)
2. `outo_models.repos.create.create_repo(kind="space")` 로 bare repo + Repo
   행 + quota 행 + `repo.create` AuditLog
3. `<spaces_dir>/<owner>/<name>.json` 사이드카 작성 (`sdk`, `updated_at`)
4. 트랜잭션 commit

## 수정

```bash
curl -X PATCH -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"description":"updated","visibility":"public"}' \
  https://models.example.com/api/spaces/alice/demo
```

`PATCH` 는 `visibility` 와 `description` 만 변경 가능합니다. `sdk` 는
v1 에서 **변경 불가** — 변경하려면 새 Space 를 만들고 옛 것을 삭제하세요.

이유: `sdk` 는 그 Space 의 "실제 런타임이 무엇인지" 에 대한 약속입니다.
조용히 바꾸면 public Space 가 자기 런타임 정보를 거짓으로 표시할 수 있어,
라이브러리는 의도적으로 변경을 거부합니다.

## 상세 조회

```bash
curl https://models.example.com/api/spaces/alice/demo
```

응답에는 다음이 포함됩니다.

```json
{
  "id": 42,
  "name": "demo",
  "sdk": "gradio",
  "visibility": "public",
  "description": "...",
  "owner": "alice",
  "clone_url": "https://models.example.com/alice/demo.git",
  "created_at": "2026-08-31T00:00:00+00:00",
  "runtime": {
    "state": "preview_unavailable",
    "message": "v1에서는 런타임이 지원되지 않습니다. 컨테이너 실행은 로드맵(v2) 항목입니다.",
    "docs_url": "/docs/spaces"
  }
}
```

`runtime.state` 는 v1 에서 항상 `preview_unavailable` 입니다. UI 는 이 값을
분기해 "데모는 v2 부터" 라는 한국어 안내와 `/docs/spaces` 링크를 표시합니다.

## 클론 / 푸시

Space 도 일반 git 저장소와 똑같이 clone / push 합니다.

```bash
git clone https://models.example.com/alice/demo.git
cd demo
echo '# Demo' > README.md
git add . && git commit -m "init"
git push -u origin main
```

권한 / 쿼터 / LFS 정책은 [git-repos.md](git-repos.md) 와 동일합니다.

## v2 런타임 (로드맵)

AGENTS.md §2.7 / README §특징 에 따라 컨테이너 런타임은 v2 로드맵 항목입니다.
v2 가 가져올 것으로 예상되는 변경점:

- `RuntimeState` 에 `running` / `sleeping` / `building` / `runtime_error` 멤버 추가
- `RuntimeStatus` 가 실제 컨테이너 상태를 조회해 채움 (k8s / podman REST API 또는
  systemd quadlet)
- 컨테이너 격리 (gVisor / nsjail / SELinux) + 자원 제한
- 빌드 큐 + 이미지 캐시

자세한 구현 일정은 본 문서를 다시 방문해 주세요. v2 가 도착하면 `SUPPORTED_SDKS`
에 새 항목이 추가되고, `runtime_status` 의 분기가 늘어납니다. v1 인터페이스
(`/api/spaces`, 사이드카 JSON) 는 그대로 유지됩니다.

## 운영 체크리스트

- Space 가 "preview_unavailable" 인데 코드만 들어 있는 경우 → 정상입니다. v1 은
  의도된 동작입니다.
- `sdk` 변경이 필요한 경우 → 새 Space 생성 후 옛 것을 삭제 (`DELETE /api/spaces/<owner>/<name>`).
- 쿼터 413 / LFS 501 / 차단된 사용자 거절 등은 [git-repos.md](git-repos.md) 와 동일.

## 다음 단계

- [git-repos.md](git-repos.md) — git 운영
- [admin.md](admin.md) — Space 가 visibility=public 일 때 admin 차단 정책
- [architecture.md](architecture.md) — Spaces 디스크 레이아웃
