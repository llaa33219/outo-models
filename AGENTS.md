# AGENTS.md — outo-models 개발 지침

이 파일은 이 저장소에서 작업하는 모든 개발자(사람 또는 AI 에이전트)가 **반드시** 따라야 하는
규칙을 정의합니다.

## 1. 프로젝트 특징

- **outo-models**는 완전 오픈소스, 자체 호스팅 가능한 모델 허브 서버입니다.
  Hugging Face / ModelScope와 유사한 기능을 목표로 하며, v1 범위는 **모델 공유 · 데이터셋 공유 ·
  Spaces** 세 가지입니다.
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
6. **LFS는 v1에서 스텁**입니다. `git lfs` 요청에는 501과 문서 링크를 반환합니다.
7. **Spaces v1은 메타데이터 + 정적 페이지**입니다. 컨테이너 런타임 실행은 로드맵(v2) 항목입니다.
8. 모든 공개 인터페이스(CLI 플래그, REST 엔드포인트, 환경 변수)는 하위 호환성을 유지합니다.
   깨야 한다면 `docs/changelog.md`에 마이그레이션 가이드를 함께 작성하세요.

## 3. 문서 업데이트 지침 (중요)

- **코드를 수정하면 같은 커밋/작업 단위에서 문서도 함께 수정합니다.** CLI 플래그 추가, 엔드포인트
  변경, 설정 항목 추가 등은 `docs/cli.md`, `docs/admin.md`, 해당 도메인 문서에 즉시 반영합니다.
- **문서와 코드가 불일치하면 문서가 틀린 것입니다.** 코드를 문서에 맞춰 되돌리지 말고 문서를
  코드에 맞게 수정하세요. 단, 코드가 의도와 다르게 동작하는 버그라면 코드를 고치고 문서는
  유지합니다 — 판단이 애매하면 이슈/논의를 남깁니다.
- 문서는 한국어로 작성하되, 코드 식별자·명령어·플래그는 원문(영문)을 유지합니다.
- `scripts/check-docs.sh`(계획됨)는 CLI/엔드포인트 심볼이 문서에 존재하는지 검사합니다.
  이 검사를 우회하지 마세요.

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
  spaces/                                 # Spaces 메타데이터 (v1)
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
3. `make lint typecheck test` 통과를 확인한다.
4. 문서 불일치가 생겼다면 **문서를** 수정한다 (§3).
5. 사용자가 명시하기 전까지 git commit/push는 하지 않는다.
