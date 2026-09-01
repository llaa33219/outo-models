# Spaces

Spaces 는 Hugging Face 의 `Spaces` 처럼 인터랙티브 데모를 호스팅하기 위한
저장소 종류입니다. v2 부터 **컨테이너 런타임** 까지 자체 지원하므로, gradio /
streamlit / docker SDK 의 데모를 빌드해서 띄울 수 있습니다. 본 문서는
[`src/outo_models/spaces/`](../src/outo_models/spaces) 의 실제 동작을 그대로
옮깁니다.

## 범위

- **저장소**: `Repo(kind="space")` — 일반 git 저장소와 동일한 인프라 사용
- **메타데이터**: `<data_dir>/spaces/<owner>/<name>.json` 사이드카 (`sdk`,
  `updated_at`)
- **REST**: `/api/spaces/*` (생성 / 목록 / 상세 / 수정 / 삭제 + 런타임 lifecycle)
- **런타임 (v2)**: Podman REST API 로 컨테이너 빌드/시작/중지/재시작/삭제.
  `OUTO_SPACES_RUNTIME_ENABLED` 로 전체 on/off.
- **프록시**: `/spaces/<owner>/<name>/run/{path:path}` — 컨테이너의 8000/tcp 로
  reverse-proxy.

`SUPPORTED_SDKS` 는 `("static", "gradio", "streamlit", "docker")` 네 가지.
생성 시 `sdk` 가 목록에 없으면 `NotFoundError("unsupported sdk: '<x>'")` 로
거절됩니다. PATCH 는 `visibility` / `description` 만 노출 — `sdk` 변경은
불가 (이 저장소가 약속한 런타임이 바뀌는 셈이므로).

## 생성

```bash
curl -X POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"name":"demo","sdk":"gradio","visibility":"public","description":"..."}' \
  https://models.example.com/api/spaces
```

응답 (`SpaceSummary`):

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

내부 순서:

1. `sdk` 가 `SUPPORTED_SDKS` 에 있는지 검증 (실패 시 `NotFoundError`)
2. `outo_models.repos.create.create_repo(kind="space")` 로 bare repo + `Repo`
   행 + quota 행 + `repo.create` 감사 로그
3. `<spaces_dir>/<owner>/<name>.json` 사이드카 작성 (`sdk`, `updated_at`)
4. 트랜잭션 commit

`SDK` 결정:

- **`static`** (기본): 컨테이너 없이 dulwich 트리를
  `<spaces_dir>/<owner>/<name>/site/` 에 풀어 `FileResponse` 로 서빙. 빌드 큐
  / Podman 호출 없음.
- **`gradio`**, **`streamlit`**: 사용자가 저장소 안에 `app.py` 등을 두고 베이스
  이미지를 정의한다는 약속만 잡고, 코드 측은 `docker` SDK 와 동일하게 처리
  (Podman 으로 빌드/시작).
- **`docker`**: 저장소 루트에 `Dockerfile` 또는 `Containerfile` 이 **없으면**
  `start` / `restart` 시 `ValidationFailedError` 로 거절.

## 상세 조회

```bash
curl https://models.example.com/api/spaces/alice/demo
```

`SpaceDetail` (`SpaceSummary` + `runtime` 블록):

```json
{
  "id": 42, "name": "demo", "sdk": "gradio",
  "visibility": "public", "description": "...",
  "owner": "alice",
  "clone_url": "https://models.example.com/alice/demo.git",
  "created_at": "2026-08-31T00:00:00+00:00",
  "runtime": {
    "state": "running",
    "message": "스페이스가 실행 중입니다.",
    "url": "https://models.example.com/spaces/alice/demo/run/",
    "container_id": "abcdef…",
    "port": 20314
  }
}
```

`runtime.state` 의 가능한다:

| state | 의미 | 후속 동작 |
| --- | --- | --- |
| `disabled` | `OUTO_SPACES_RUNTIME_ENABLED=false` (운영자가 끌 때) | start/stop/restart 모두 503 |
| `stopped` | 컨테이너가 없거나 exited/stopped | start 로 띄울 수 있음 |
| `building` | `podman build` 가 진행 중인 상태 (Podman 응답에서 추론) | 잠시 후 `running` / `failed` |
| `running` | 컨테이너가 `running` 이고 호스트 포트가 잡힘 | `/spaces/<owner>/<name>/run/` 으로 접근 |
| `failed` | Podman 호출이 실패했거나 컨테이너가 비정상 | audit 로그의 `space.<action>` 에 error_code 기록 |

## 런타임 lifecycle

세 개의 POST 엔드포인트가 컨테이너를 조작합니다. 모두 인증은 현재 세션
(쿠키 + `get_current_user`) 만 받습니다 — PAT 만으로 호출하지 마세요.

| 엔드포인트 | 메서드 | 동작 |
| --- | --- | --- |
| `/api/spaces/{owner}/{name}/start` | `POST` | `build_image()` → `start()`. 정적 SDK 는 `export_static_site` 만 |
| `/api/spaces/{owner}/{name}/stop` | `POST` | `manager.stop()` — Podman `containers/{name}/stop?t=0` |
| `/api/spaces/{owner}/{name}/restart` | `POST` | 정적 SDK 는 `export_static_site` 재실행, 그 외는 stop → build_image → start |
| `/api/spaces/{owner}/{name}/status` | `GET` | Podman inspect 결과를 `RuntimeStatus` 로 매핑 (익명도 조회 가능) |

각 액션은:

1. `_ensure_runtime_enabled(settings)` — 비활성이면 `503 runtime_disabled`
2. `Repo` 로드 + owner/admin 검증
3. `REPO_LOCKS.acquire(owner, name)` 으로 같은 스페이스의 동시 액션 직렬화
4. `SpaceRuntimeManager` 의 메서드 호출
5. `AuditLog(action="space.<action>", detail={ok, state/error_code})` 기록

라이프사이클 메서드의 정확한 REST 경로는
[`Podman REST 경로`](#podman-rest-경로) 참고.

### `start` 응답 예

```bash
curl -X POST -b cookies.txt \
  https://models.example.com/api/spaces/alice/demo/start
```

```json
{
  "state": "running",
  "message": "스페이스가 실행 중입니다.",
  "url": "https://models.example.com/spaces/alice/demo/run/",
  "container_id": "abcdef…",
  "port": 20314
}
```

`port` 는 `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` (기본 20000..21000) 에서
순차 할당된 호스트 포트. 컨테이너 안 포트는 `8000/tcp` 으로 고정입니다.

### `run/` 프록시

컨테이너가 `running` 일 때 다음 5개 메서드를 모두 지원합니다.

```
GET    /spaces/<owner>/<name>/run/{path:path}
POST   /spaces/<owner>/<name>/run/{path:path}
PUT    /spaces/<owner>/<name>/run/{path:path}
PATCH  /spaces/<owner>/<name>/run/{path:path}
DELETE /spaces/<owner>/<name>/run/{path:path}
```

- hop-by-hop 헤더 (`connection`, `keep-alive`, `transfer-encoding`, `host`,
  `content-length` 등) 와 `Authorization` 류는 제거하고 위임
- 컨테이너가 running 이 아니면 `503 space_not_running`
- 위임 대상이 죽었으면 `504 proxy_unreachable` (httpx `RequestError`)
- `static` SDK 는 이 라우트가 컨테이너로 가지 않고 `static_site_dir` 의 파일을
  직접 `FileResponse` 로 반환 (`{path}` 가 빈 문자열 / `/` 로 끝나면
  `index.html` 로 폴백)

## GPU 할당

`web_settings(key="gpu:<username>")` 의 JSON 배열을 컨테이너 생성 시
`nvidia.com/gpu=<id>` CDI 디바이스로 부착합니다. 운영자가
`outo-models admin gpu assign alice gpu-0` 로 할당하고, `start` 호출 시 그
사용자의 GPU 목록이 컨테이너에 전달됩니다.

- CDI 디바이스가 없는 호스트에서는 Podman 이 디바이스 등록을 거부하고
  `OutoError(code="podman_api", status_code=502)` 가 납니다.
- 한 사용자에게 여러 GPU 를 할당하면 (`gpu-0 gpu-1`) 둘 다 부착됩니다.
- 빌드는 GPU 가 없는 호스트에서 일어나므로, GPU 할당은 빌드 단계가 아니라
  런타임 컨테이너에만 영향을 줍니다.

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

## 설정 (`OUTO_*`)

| 환경 변수 | 의미 | 기본 |
| --- | --- | --- |
| `OUTO_SPACES_RUNTIME_ENABLED` | 런타임 활성화 여부 | `false` |
| `OUTO_PODMAN_SOCKET` | Podman REST API Unix 소켓 경로 | `/run/podman/podman.sock` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` | 호스트 포트 범위 시작 | `20000` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_END` | 호스트 포트 범위 끝 | `21000` |

운영 환경에서는 컨테이너를 띄울 때 호스트의 Podman 소켓을 마운트해야 합니다.

```bash
# rootless Podman (사용자 1000) 의 user socket
podman run -d --name outo-models \
  -v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro \
  -e OUTO_SPACES_RUNTIME_ENABLED=true \
  ...
```

> **소켓 마운트는 신뢰 경계입니다.** 호스트의 Podman 데몬은 컨테이너 내부
> 프로세스에 컨테이너 생성/삭제 권한을 그대로 부여합니다. 비특권 컨테이너
> + 읽기 전용 마운트 + 호스트 측 namespace 격리 (userns) 조합이 안전의
> 최소 조건입니다. 자세한 내용은
> [security.md §Spaces runtime 격리](security.md#spaces-runtime-격리) 참고.

## Podman REST 경로

`SpaceRuntimeManager` ([`spaces/runtime_manager.py`](../src/outo_models/spaces/runtime_manager.py))
는 다음 경로만 호출합니다 (`/v4.0.0/libpod` 접두사 기준).

| 동작 | HTTP |
| --- | --- |
| 점유 검사 | `GET /v4.0.0/libpod/containers/json?all=true&filter=label=outo.managed=true` |
| 컨테이너 생성 | `POST /v4.0.0/libpod/containers/create` |
| 시작 | `POST /v4.0.0/libpod/containers/{name}/start` |
| 중지 | `POST /v4.0.0/libpod/containers/{name}/stop?t=0` |
| 재시작 | `POST /v4.0.0/libpod/containers/{name}/restart` |
| 강제 삭제 | `DELETE /v4.0.0/libpod/containers/{name}?force=true&ignore=true` |
| inspect | `GET /v4.0.0/libpod/containers/{name}/json` |
| 이미지 빌드 | `POST /v4.0.0/libpod/build?t=<tag>` (Content-Type `application/x-tar`) |

이미지는 `localhost/outo-space-<owner>-<name>:latest` 태그로 저장되고, 컨테이너
이름은 `outo-space-<owner>-<name>` 입니다. 두 네임스페이스는
`outo.managed=true` + `outo.space=<owner>/<name>` 레이블로만 관리됩니다 — 호스트
Podman 의 다른 컨테이너/이미지와 충돌하지 않습니다.

## 트러블슈팅

자세한 오류 처리는 [troubleshooting.md](troubleshooting.md) 의 다음 섹션 참고.

- **503 `runtime_disabled`** — `OUTO_SPACES_RUNTIME_ENABLED=true` 가 컨테이너에
  전달되지 않은 경우. `podman inspect outo-models --format '{{.Config.Env}}'` 로 확인.
- **503 `podman_unreachable`** — Podman 소켓이 마운트되지 않았거나 권한 부족.
  컨테이너 안에서 `curl --unix-socket /run/podman/podman.sock
  http://d/v4.0.0/libpod/containers/json` 으로 도달 가능한지 확인.
- **502 `space_build_failed`** — Podman 빌드 실패. 응답 message 의 마지막
  2 KiB 가 Podman 빌드 로그의 일부 (`_build_failure_tail`) 이므로 Dockerfile /
  Containerfile 을 그 메시지와 함께 디버깅.
- **503 `space_not_running`** — `/run/` 프록시 호출 시 컨테이너가 running 이
  아님. `/api/spaces/<owner>/<name>/status` 로 상태 확인 후 `start`.
- **503 "모든 사용 가능한 런타임 포트가 사용 중"** —
  `OUTO_SPACES_RUNTIME_PORT_RANGE_END` 값을 늘리거나 사용하지 않는 스페이스를
  `stop` 하세요.

## 다음 단계

- [git-repos.md](git-repos.md) — clone / push 와 LFS 흐름
- [security.md](security.md) — Spaces 런타임 격리 / Podman 소켓 위험
- [troubleshooting.md](troubleshooting.md) — Podman/LFS 오류 응답 모음