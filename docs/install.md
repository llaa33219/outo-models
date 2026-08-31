# 설치

`outo-models` 는 **단일 Podman 이미지**로 배포됩니다. 컨테이너 안에서 FastAPI
앱과 Caddy가 함께 실행되며, 데이터 디렉터리와 설정 파일만 호스트에 남습니다.
이 페이지에서는 이미지 빌드부터 첫 컨테이너 실행까지의 전 과정을 다룹니다.

> **개발 환경에서는 podman 이 없습니다** (AGENTS.md §4). 빌드와 실제 컨테이너
> 동작 검증은 별도의 테스트 머신에서 수행해 주세요. 본 저장소의 개발 환경
> 에서는 `uv sync` → `make lint` → `make typecheck` → `make test` 까지만
> 보장합니다.

## 1. 사전 준비

운영 환경에 해당하는 서버 호스트에서 다음을 확인하세요.

- **podman** 4.x 이상 (`podman --version` 으로 확인)
- **firewalld / ufw / nftables** 중 하나 (없으면 [troubleshooting.md](troubleshooting.md)
  의 "방화벽 미감지 호스트" 섹션 참고)
- 80 / 443 포트 외부 노출 가능 (클라우드 보안 그룹 포함)
- DNS 위임이 끝난 도메인 (예: `models.example.com`)
- 선택: ACME 발급을 위한 연락 받을 이메일
- 선택: DNS 자동 모드를 쓸 Cloudflare API 토큰 (권한: `Zone.DNS:Edit`)

## 2. 이미지 빌드

이미지는 두 가지 플레이버로 빌드합니다. 빌드 인자 `IMAGE_FLAVOR` 가
`stable` 또는 `dev` 가 아니면 빌드가 즉시 실패합니다.

```bash
# 운영용 (비특권, 디버그 도구 없음)
make build-stable

# 개발용 (debugpy / ipython 포함, OUTO_ENV=development)
make build-dev
```

`make` 는 내부적으로 다음을 실행합니다.

```bash
podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .
podman build --build-arg IMAGE_FLAVOR=dev    -t outo-models:dev    .
```

빌드 단계는 [Containerfile](../Containerfile) 에 정의되어 있습니다. 핵심은
다음과 같습니다.

- `uv sync --frozen --no-dev --no-editable` 로 의존성 잠금
- `xcaddy build --with github.com/caddy-dns/cloudflare` 로 Caddy + DNS-01 플러그인
- `runtime-base` 단계에서 `IMAGE_FLAVOR` 검증 + 비특권 사용자 (uid/gid 1000) 생성
- `stable` / `dev` 단계에서 환경 변수 / 추가 패키지 분기

`dev` 플레이버를 프로덕션에 배포하지 마세요. 엔트리포인트가
`IMAGE_FLAVOR=dev` + `OUTO_ENV=production` 조합은 거부합니다 (AGENTS.md §4).

## 3. 컨테이너 외부 데이터 디렉터리

기본 데이터 디렉터리는 `/var/lib/outo-models` 입니다. 호스트에 미리 만들고
권한을 잡아 주세요.

```bash
sudo mkdir -p /var/lib/outo-models
sudo chown -R 1000:1000 /var/lib/outo-models
```

`setup` 위저드가 이 디렉터리에 `db.sqlite3`, `repos/`, `spaces/`, `certs/`,
`audit/` 를 만듭니다. 자세한 내용은
[architecture.md](architecture.md#데이터-레이아웃) 를 보세요.

## 4. 첫 실행: 설정 마법사

이미지를 빌드한 직후, 설정 파일을 만들기 위해 한 번 **호스트에서** 마법사를
실행합니다.

```bash
sudo outo-models setup
```

이 명령은 다음을 차례로 수행합니다.

1. 도메인 / ACME 이메일 입력
2. DNS 제공자 선택 (`cloudflare` / `manual`)
3. 공개 IPv4 입력 (또는 자동 감지)
4. 관리자 계정 생성
5. `config.yaml` 작성 (mode `0o600`)
6. DNS A 레코드 생성 (또는 수동 안내)
7. 호스트 방화벽에 80 / 443 개방
8. DB 마이그레이션 + 관리자 비밀번호 해시 저장
9. Caddyfile 렌더링

전체 흐름은 [setup-wizard.md](setup-wizard.md) 에 있습니다.

비대화형 모드로 자동화하려면 다음 예시처럼 플래그를 지정합니다.

```bash
sudo outo-models setup --non-interactive \
  --domain models.example.com \
  --acme-email admin@example.com \
  --dns-provider cloudflare \
  --public-ipv4 203.0.113.10 \
  --admin-username admin \
  --admin-email admin@example.com \
  --admin-password '<운영자가 직접 생성한 안전한 비밀번호>' \
  --yes
```

Cloudflare 모드에서는 `--admin-password` 와 같은 방식으로 토큰이 필요합니다.
`OUTO_CLOUDFLARE_API_TOKEN` 환경 변수가 우선 적용됩니다.

## 5. 컨테이너 시작

설정이 끝났으면 호스트에서 다음 한 줄로 컨테이너를 띄울 수 있습니다.

```bash
sudo outo-models start
```

내부적으로 다음을 실행합니다.

```bash
podman run -d --name outo-models \
  -e OUTO_DATA_DIR=/var/lib/outo-models \
  -e OUTO_SECRET_KEY=... \
  -e OUTO_DOMAIN=models.example.com \
  -e OUTO_REQUIRE_APPROVAL=true \
  -e OUTO_DB_URL=... (선택) \
  -v outo-models-data:/var/lib/outo-models \
  --cap-add NET_BIND_SERVICE \
  -p 80:80 -p 443:443 \
  outo-models:stable
```

`start` 명령은 `/etc/outo-models/config.yaml` 의 `image`, `volume`, `ports`
키를 읽어 그대로 전달합니다. 컨테이너 내부 엔트리포인트
(`/usr/local/bin/outo-entrypoint.sh`) 는 한국어 배너를 출력한 뒤
`outo-models serve` 로 `exec` 합니다. 자세한 요청 흐름은
[architecture.md](architecture.md#요청-흐름) 를 보세요.

이상이 없으면 다음 명령으로 컨테이너가 떠 있는지 확인합니다.

```bash
outo-models status
# [상태] 실행 중: outo-models
```

이제 `https://models.example.com/` 으로 접속할 수 있고, 첫 로그인은
`setup` 단계에서 만든 관리자 계정으로 하면 됩니다.

## 6. 설치 후 점검

운영 시작 전 다음을 점검하세요.

- `https://<도메인>/admin` 페이지가 보임 (관리자 로그인 필요)
- `https://<도메인>/api/admin/users` 가 admin PAT 으로 200 응답
- `git clone https://<도메인>/<관리자>/test.git` 후 첫 push 가 통과
  ([git-repos.md](git-repos.md) 참고)
- `outo-models status` 가 `[상태] 실행 중` 으로 표시

문제가 있다면 [troubleshooting.md](troubleshooting.md) 를 참고하세요.

## 7. 업그레이드

`outo-models update` 한 줄로 새 이미지를 받아 마이그레이션 후 컨테이너를
재시작합니다. 자세한 흐름은 [cli.md](cli.md#update) 와
[architecture.md](architecture.md#이미지-플레이버) 를 보세요.

```bash
sudo outo-models update --image outo-models:stable
```

## 다음 단계

- [setup-wizard.md](setup-wizard.md) — 마법사가 어떤 일을 하는지 정확하게
- [admin.md](admin.md) — 가입 승인 / 쿼터 / GPU 운영
- [architecture.md](architecture.md) — 데이터 레이아웃과 요청 흐름
