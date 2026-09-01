# AGENTS.md — outo-models 개발 지침

이 파일은 이 저장소에서 작업하는 모든 개발자(사람 또는 AI 에이전트)가 **반드시** 따라야 하는
규칙을 정의합니다.

## 1. 프로젝트 특징

- **outo-models**는 완전 오픈소스, 자체 호스팅 가능한 모델 허브 서버입니다.
  Hugging Face / ModelScope와 유사한 기능을 목표로 하며, v2 범위는 **모델 공유 · 데이터셋 공유 ·
  Spaces · Git LFS** 네 가지입니다.
- Python 3.12 + FastAPI + SQLAlchemy(async) + dulwich(git smart-HTTP) + Caddy(자동 HTTPS/ACME)
  로 구성되며, **Podman 단일 이미지**로 배포합니다.
- 저장소는 모두 git으로 클론/푸시할 수 있습니다(`git clone https://<도메인>/<소유자>/<이름>.git`).
- 서버 운영자는 `outo-models` CLI 하나로 서버를 관리합니다:
  - `setup` — 최초 대화형 설정 (도메인, DNS 제공자, 관리자 계정, 포트)
  - `start` / `stop` / `restart` / `status`
  - `reset` — 모든 데이터를 삭제하고 최초 설치 상태로 되돌림. **경고 후 "yes"를 정확히 3번
    입력해야만 실행됩니다.** 이 안전장치는 어떤 이유로도 약화시키지 마세요.
  - `update` — 이미지 갱신 + DB 마이그레이션 + 재시작을 자동 수행
  - `admin ...` — 가입 승인/거절, 사용자 차단/해제, 저장공간 할당량, GPU 할당 등
- "자동"이 이 프로젝트의 핵심 가치입니다. 설치 후 사용자가 수동으로 해야 하는 일이 새로
  생기는 변경은 설계 결함으로 간주합니다.

## 2. 개발 주의점

1. **보안 타협 금지.** 비밀번호는 argon2, API 토큰은 PASETO v4, 토큰 원문은 절대 저장하지
   않습니다(해시된 지문만 저장). `as any`/`type: ignore` 남발, 빈 `except`, 평문 시크릿 로깅은
   모두 금지입니다.
2. **reset 안전장치 불변.** 3회 "yes" 확인 로직과 dry-run 기본 동작을 변경하는 PR은 거부됩니다.
3. **컨테이너는 비특권(non-root)으로 실행됩니다.** 방화벽 개방 등 호스트 권한이 필요한 작업은
   컨테이너 남쪽이 아니라 **호스트 측 스크립트**(`container/scripts/`)가 CLI를 통해 수행합니다.
4. **SQLite가 기본 DB**이지만 SQLAlchemy를 통해 Postgres 전환이 가능해야 합니다. DB 특화 SQL을
   직접 쓰지 마세요.
5. **동시 푸시**: 저장소 쓰기는 per-repo `asyncio.Lock`으로 직렬화하고, 사용자 사용량은
   주기적 reconcile job으로 보정합니다.
6. **LFS는 v2에서 실구현입니다.** `git lfs` 요청은 [`src/outo_models/git_smart/lfs.py`](src/outo_models/git_smart/lfs.py)
   + [`lfs_api.py`](src/outo_models/git_smart/lfs_api.py) 의 4개 엔드포인트(`/info/lfs/objects/batch`,
   `PUT/GET /info/lfs/objects/{oid}`)로 처리됩니다. 모든 LFS 객체는
   [`src/outo_models/objectstore/`](src/outo_models/objectstore) 의 `ObjectStore` 프로토콜을 통해
   저장되며, `OUTO_LFS_BACKEND` (`local` 기본 / `s3`) 가 구현체를 결정합니다.
   - `local` 백엔드는 `data_dir/lfs/<aa>/<bb>/<oid>` 에 샤딩 저장하고, sha256 + 사이즈 검증 후
     `os.replace` 로 원자적 교체. PUT/GET 은 컨테이너 안에서 직접 스트리밍 (Basic 인증 재사용).
   - `s3` 백엔드는 자체 구현 SigV4 (path-style, MinIO 호환) 로 presigned URL 을 만들어
     클라이언트가 직접 업로드/다운로드합니다. PUT/GET 핸들러는 S3 백엔드 사용 시 `501` 을
     반환 (proxy 업로드는 v3).
   - `OutoError("LFS locks are not supported yet")` 만 501 유지. `/info/lfs/locks*` 는 v3.
   - `lfs_max_object_bytes` 와 사용자 쿼터가 batch 응답의 **per-object error** 로 표현되며,
     한 객체의 실패가 전체 batch 를 실패시키지 않습니다.
7. **Spaces v2 런타임**은 [`src/outo_models/spaces/runtime.py`](src/outo_models/spaces/runtime.py),
   [`runtime_manager.py`](src/outo_models/spaces/runtime_manager.py),
   [`build.py`](src/outo_models/spaces/build.py) 의 Podman REST 클라이언트를 통해 동작합니다.
   - 기본은 **비활성** (`OUTO_SPACES_RUNTIME_ENABLED=false`). 활성화는 운영자가 명시적으로
     해야 하며, 그 순간부터 컨테이너 안에서 Podman API 소켓
     (`OUTO_PODMAN_SOCKET`, 기본 `/run/podman/podman.sock`) 이 도달 가능해야 합니다.
   - 컨테이너는 **비특권** (uid 1000) 으로 실행되므로 Podman 소켓을 호스트에서 마운트
     (`-v /run/user/1000/podman/podman.sock:/run/podman/podman.sock:ro` 등) 해 주세요.
     rootless Podman 의 user socket 이 표준 위치입니다.
   - 호스트 포트는 `OUTO_SPACES_RUNTIME_PORT_RANGE_START..END` 범위에서 `list_managed()` 로
     점유 검사 후 순차 할당. 컨테이너 안 포트 `8000/tcp` 만 호스트로 노출하고, IP 는
     `127.0.0.1` 로 바인딩 (외부 노출 금지).
   - 컨테이너 식별자는 `outo-space-<owner>-<name>`, 이미지는
     `localhost/outo-space-<owner>-<name>:latest`, 레이블은 `outo.managed=true` +
     `outo.space=<owner>/<name>`. 라이프사이클은 `podman rm -f` 와 직접 호환되지 않는
     Podman REST 경로로만 (`v1/.../containers/{name}{create,start,stop,restart,remove,json}`).
   - `static` SDK 는 컨테이너를 띄우지 않고 dulwich 트리를
     `<spaces_dir>/<owner>/<name>/site/` 에 풀어 `FileResponse` 로 서빙합니다
     (`make_build_context` 와 `export_static_site` 가 같은 `_iter_tree_blobs` 를 공유).
   - `docker` SDK 는 저장소 루트에 `Dockerfile` 또는 `Containerfile` 이 **없으면
     `ValidationFailedError`** 로 거절 (`build_image` 호출 전 검증).
   - `gradio` / `streamlit` SDK 는 컨테이너 내부에서 사용자가 베이스 이미지를 정의한다는
     약속만 잡고, 코드 측에는 `Dockerfile`/`Containerfile` 강제와 동일하게 동작합니다.
   - GPU 는 `web_settings(key="gpu:<username>")` 의 JSON 배열을 읽어
     `nvidia.com/gpu=<id>` CDI 디바이스로 컨테이너에 부착합니다. CDI 가 없는 환경에서는
     Podman 이 디바이스를 거부하므로, 운영자가 호스트에 nvidia-container-toolkit + CDI
     사양을 설치해야 합니다.
   - 프록시 라우트 `/spaces/<owner>/<name>/run/{path}` 는 컨테이너가 running 일 때만
     `http://127.0.0.1:<host_port>/<path>` 로 reverse-proxy 합니다. hop-by-hop 헤더와
     `Content-Length` 를 제거하고, 실패 시 `503 space_not_running` / `504 proxy_unreachable`
     으로 응답합니다.
8. 모든 공개 인터페이스(CLI 플래그, REST 엔드포인트, 환경 변수)는 하위 호환성을 유지합니다.
   깨야 한다면 `docs/changelog.md`에 마이그레이션 가이드를 함께 작성하세요.

## 3. 문서 업데이트 지침 (중요)

- **코드를 수정하면 같은 커밋/작업 단위에서 문서도 함께 수정합니다.** CLI 플래그 추가, 엔드포인트
  변경, 설정 항목 추가 등은 `docs/cli.md`, `docs/admin.md`, 해당 도메인 문서에 즉시 반영합니다.
- **문서와 코드가 불일치하면 문서가 틀린 것입니다.** 코드를 문서에 맞춰 되돌리지 말고 문서를
  코드에 맞게 수정하세요. 단, 코드가 의도와 다르게 동작하는 버그라면 코드를 고치고 문서는
  유지합니다 — 판단이 애매하면 이슈/논의를 남깁니다.
- 문서는 한국어로 작성하되, 코드 식별자·명령어·플래그는 원문(영문)을 유지합니다.
- `scripts/check-docs.sh` 는 CLI 명령, REST 라우터 심볼, `OUTO_*` 환경 변수가 문서에
  존재하는지 검사합니다. 이 검사를 우회하지 마세요. CI 의 `Docs/code parity` 단계에서도
  강제됩니다 (`.github/workflows/ci.yml`).

## 4. 개발 환경과 테스트 환경의 분리 (중요)

- **현재 작업 환경은 개발(development) 환경입니다.** 이 머신에는 podman이 없으며, 여기서
  이미지를 빌드/실행해 "동작 확인"했다고 주장하지 마세요.
- 개발 환경에서의 검증은 다음까지입니다: `uv sync`, `make lint`, `make typecheck`, `make test`
  (단위/통합 테스트는 실제 `git` 바이너리와 `httpx` 기반으로 컨테이너 없이 실행됩니다).
- **실제 배포 테스트는 별도의 테스트 컴퓨터에서 Podman 이미지로 진행합니다.** 개발 환경에서
  `podman build/run`이 안 된다고 코드를 바꾸지 말고, Containerfile은 정적 검토(hadolint,
  경로/권한 점검)로 검증합니다.
- 이미지는 두 가지 플레이버로 존재합니다:
  - `outo-models:stable` — 프로덕션용. 비특권 실행, 디버그 도구 없음.
  - `outo-models:dev` — 개발용. debugpy/ipython 포함, `OUTO_ENV=development`.
  - 빌드: `make build-stable` / `make build-dev` (테스트 머신에서 실행).
- `dev` 이미지를 프로덕션에 배포하면 안 됩니다. entrypoint가
  `IMAGE_FLAVOR=dev` + `OUTO_ENV=production` 조합을 거부하도록 유지하세요.

## 5. 코드베이스 지도

```
src/outo_models/
  config.py, logging.py, exceptions.py   # 코어 인프라
  utils/                                  # 경로, 슬러그, 시간, 해시 유틸
  auth/                                   # argon2, 세션, PASETO PAT, 권한, 레이트리밋, 가입승인
  db/                                     # SQLAlchemy 모델 + Alembic 마이그레이션
  dns/                                    # DNSProvider 추상화 (cloudflare, manual)
  firewall/                               # firewalld/ufw/nft 감지 + 호스트 스크립트 호출
  tls/                                    # Caddyfile 렌더링 + 리로드 + 갱신 헬스체크
  tasks/                                  # APScheduler 잡 (인증서, 쿼터 reconcile, 감사로그 정리)
  repos/                                  # 저장소 디스크 레이아웃, 생성/삭제, 쿼터
  spaces/                                 # Spaces 메타데이터 + v2 컨테이너 런타임
    registry.py                            # SDK 사이드카 + CRUD
    runtime.py                             # RuntimeState / Status 매핑
    runtime_manager.py                     # Podman REST 클라이언트
    build.py                               # dulwich 트리 → tar + 정적 사이트 export
  objectstore/                             # LFS ObjectStore 프로토콜 + 백엔드
    base.py                                # ObjectStore + LfsAction
    local.py                               # 디스크 백엔드 (스트리밍 PUT/GET)
    s3.py                                  # S3 백엔드 (자체 SigV4, MinIO 호환)
    factory.py                             # OUTO_LFS_BACKEND 디스패치
  git_smart/                              # dulwich 기반 git smart-HTTP 서비스
  server/                                 # FastAPI 앱, 라우터, 미들웨어, Jinja 템플릿
  cli/                                    # `outo-models` Typer CLI
  cli_remote/                             # CLI → 관리 REST 클라이언트
container/                                # rootfs, Caddyfile 템플릿, 호스트 스크립트, systemd 예시
docs/                                     # 한국어 상세 문서
tests/                                    # unit / integration / fixtures
```

## 6. 작업 절차

1. 변경 전 관련 문서(docs/)를 먼저 읽는다.
2. 테스트를 먼저 쓰거나(TDD) 최소한 같은 커밋에 테스트를 포함한다.
3. `make lint typecheck test` 통과 + `.github/workflows/ci.yml` 의 `Docs/code parity`
   단계(`bash scripts/check-docs.sh`) 통과를 확인한다.
4. 문서 불일치가 생겼다면 **문서를** 수정한다 (§3). 새 `OUTO_*` 환경 변수나 CLI 플래그를
   추가했다면 `scripts/check-docs.sh` 가 강제하는 문서 표기 위치도 함께 갱신한다.
5. 사용자가 명시하기 전까지 git commit/push는 하지 않는다.
6. **이미지 릴리즈는 `.github/workflows/release-image.yml` 의 태그 컨벤션을 따른다.**
   `vX.Y.Z-stable` 태그 → `ghcr.io/<repo>:X.Y.Z-stable` + `:stable` + `:latest` (stable
   만), `vX.Y.Z-dev` 태그 → `ghcr.io/<repo>:X.Y.Z-dev` + `:dev`. 컨테이너 빌드는
   `podman build --build-arg IMAGE_FLAVOR=stable|dev ...` 만 사용한다 (AGENTS.md §4 의
   dev/prod 조합 가드를 우회하지 말 것).
