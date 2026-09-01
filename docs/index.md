# outo-models 문서

outo-models는 Hugging Face / ModelScope 스타일의 git 기반 모델 허브를 자체 호스팅할
수 있도록 해주는 단일 Podman 이미지로 배포되는 서버입니다. 본 문서 묶음은
운영자가 설치부터 일 운영, 장애 대응까지 모두 자체적으로 처리할 수 있도록 쓰여
있습니다.

문서와 코드가 서로 어긋난다면 **문서**가 틀린 것입니다 (AGENTS.md §3). 본
저장소는 `scripts/check-docs.sh` 로 CLI 명령, 환경 변수, 문서 간 일치를
자동 검증합니다.

## 목차

- [설치](install.md) — 이미지 pull / 빌드와 첫 실행
- [설정 마법사](setup-wizard.md) — `outo-models setup` 의 모든 프롬프트와 자동 처리
- [CLI 레퍼런스](cli.md) — 모든 명령·플래그·환경 변수의 단일 진실 공급원
- [관리자 가이드](admin.md) — 가입 승인·차단·쿼터·GPU·원격 모드
- [아키텍처](architecture.md) — 모듈 지도, 요청 흐름, 데이터 레이아웃, CI/CD
- [보안](security.md) — argon2, PASETO PAT, 세션, CSRF, 레이트리밋, LFS, Spaces 격리
- [DNS 제공자](dns-providers.md) — Cloudflare 자동 모드와 수동 모드
- [git 저장소 사용법](git-repos.md) — clone / push, PAT 사용, 쿼터, LFS 정책
- [Spaces](spaces.md) — v2 런타임 lifecycle, Podman 통합, GPU, 프록시
- [문제 해결](troubleshooting.md) — 자주 부딪히는 운영 이슈 (Podman, LFS, S3 포함)
- [테스트](testing.md) — `make lint/typecheck/test/smoke` 와 통합 테스트 범위
- [변경 이력](changelog.md) — v0.1.0 · v0.2.0 릴리즈 노트

## 빠른 시작

서버 호스트에 podman이 설치되어 있다고 가정합니다 (`podman --version` 으로
확인). 자세한 내용은 [install.md](install.md) 를 참고하세요.

```bash
# 1) 이미지 가져오기 (권장: ghcr.io 의 stable)
sudo podman pull ghcr.io/<owner>/outo-models:stable

# 또는 자체 빌드 (테스트 머신에서)
make build-stable          # outo-models:stable
make build-dev             # outo-models:dev (개발용)

# 2) 초기 설정 (대화형 마법사)
outo-models setup

# 3) 운영
outo-models start
outo-models status
outo-models restart
outo-models update

# 4) 전체 초기화 (3회 yes 확인 필요)
outo-models reset --destroy      # OUTO_DESTRUCTIVE=1 과 함께
```

`setup` 이 끝나면 `https://<도메인>/` 으로 들어갈 수 있고, git 클라이언트는
`https://<도메인>/<소유자>/<이름>.git` 으로 clone / push 합니다. 자세한
흐름은 [setup-wizard.md](setup-wizard.md) 와 [git-repos.md](git-repos.md) 를
읽어 주세요.

## 환경 변수 빠른 참조

본문서에서 자주 등장하는 환경 변수의 목록입니다. 모든 `OUTO_*` 환경 변수의
완전한 정의는 [CLI 레퍼런스](cli.md#환경-변수) 를 보세요.

| 변수 | 의미 | 기본값 |
| --- | --- | --- |
| `OUTO_DATA_DIR` | 데이터 디렉터리 (DB, git 저장소, LFS, 인증서 캐시) | `/var/lib/outo-models` |
| `OUTO_DOMAIN` | 서비스가 응답할 공개 도메인 | `localhost` |
| `OUTO_DB_URL` | DB URL (빈 값이면 `${OUTO_DATA_DIR}/db.sqlite3`) | (파생) |
| `OUTO_SECRET_KEY` | 세션 / 토큰 서명 키 (production 에서 32자 이상) | (없음) |
| `OUTO_ENV` | 런타임 환경 (`development` / `production`) | `development` |
| `OUTO_REQUIRE_APPROVAL` | 신규 가입 시 관리자 승인 필요 여부 | `true` |
| `OUTO_DEFAULT_QUOTA_BYTES` | 신규 사용자에게 부여하는 기본 저장공간 | `10737418240` (10 GiB) |
| `OUTO_LFS_BACKEND` | LFS 백엔드 (`local` / `s3`) | `local` |
| `OUTO_LFS_MAX_OBJECT_BYTES` | LFS 단일 객체 최대 크기 | `5368709120` (5 GiB) |
| `OUTO_S3_ENDPOINT` / `OUTO_S3_BUCKET` / `OUTO_S3_REGION` | S3 백엔드 endpoint, 버킷, region | (없음 / 없음 / `us-east-1`) |
| `OUTO_S3_ACCESS_KEY` / `OUTO_S3_SECRET_KEY` | S3 자격 증명 — 환경 변수로만 주입 | (없음) |
| `OUTO_S3_PREFIX` / `OUTO_S3_PRESIGN_TTL_SECONDS` | S3 객체 키 접두사, presign TTL | `lfs` / `3600` |
| `OUTO_SPACES_RUNTIME_ENABLED` | Spaces 컨테이너 런타임 on/off | `false` |
| `OUTO_PODMAN_SOCKET` | Podman REST API Unix 소켓 | `/run/podman/podman.sock` |
| `OUTO_SPACES_RUNTIME_PORT_RANGE_START` / `_END` | Space 컨테이너 호스트 포트 범위 | `20000` / `21000` |
| `OUTO_CONFIG` | YAML 설정 파일 경로 (CLI 호스트 측에서 사용) | `/etc/outo-models/config.yaml` |
| `OUTO_DESTRUCTIVE` | `reset --destroy` 의 안전 게이트 통과 조건 | (없음) |
| `OUTO_CLOUDFLARE_API_TOKEN` | Cloudflare 모드에서 DNS 레코드 생성에 사용 | (없음) |
| `OUTO_CADDY_ADMIN_URL` | Caddy 관리 API 베이스 URL | `http://localhost:2019` |
| `CLOUDFLARE_API_TOKEN` | Caddy 프로세스 안에서 DNS-01 챌린지에 사용 | (없음) |

## 다음 단계

- 처음 설치하는 운영자라면 → [install.md](install.md) → [setup-wizard.md](setup-wizard.md)
- 사용자에게 권한·저장공간·GPU 를 부여하는 운영자라면 → [admin.md](admin.md)
- git 저장소로 모델을 업로드하려는 사용자라면 → [git-repos.md](git-repos.md) (LFS 사용법 포함)
- Spaces 를 만들고 싶다면 → [spaces.md](spaces.md) (Podman 런타임)
- 문제 상황에 부딪혔다면 → [troubleshooting.md](troubleshooting.md) (Podman / LFS / S3 포함)
- 릴리즈 노트를 빠르게 훑어보고 싶다면 → [changelog.md](changelog.md)