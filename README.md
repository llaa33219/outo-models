# outo-models

완전 오픈소스, 자체 호스팅 가능한 모델 허브 서버입니다. Hugging Face / ModelScope와 유사하게
**모델 · 데이터셋 · Spaces** 를 git 기반으로 공유할 수 있으며, 설치 후 포트 개방 · HTTPS 인증서
발급/갱신 · DNS 레코드 설정 · 업데이트까지 모두 자동으로 처리합니다.

## 특징

- **완전 자동 설치**: `outo-models setup` 하나로 방화벽 개방, ACME(Let's Encrypt) HTTPS 인증서
  발급/자동 갱신, DNS 레코드 자동 설정(Cloudflare 플러그인 + 수동 모드)까지 완료됩니다.
- **git 네이티브 저장소**: 모델/데이터셋/Space 저장소를 `git clone`, `git push` 로 그대로 사용합니다.
- **회원 관리**: 회원가입/로그인, 관리자 승인제 가입(토글 가능), 사용자 차단, 저장공간 할당량,
  GPU 할당 등 관리 명령어를 제공합니다.
- **보안 우선**: argon2 비밀번호 해시, PASETO v4 API 토큰, 보안 헤더, 레이트 리밋, 감사 로그.
- **Podman 단일 이미지 배포**: `stable` / `dev` 두 가지 이미지 플레이버.

## 빠른 시작

```bash
# 이미지 빌드 (테스트 머신에서)
podman build --build-arg IMAGE_FLAVOR=stable -t outo-models:stable .

# 초기 설정 (대화형 마법사: 도메인, DNS, 관리자 계정 등)
outo-models setup

# 운영
outo-models start
outo-models restart
outo-models status
outo-models update

# 전체 초기화 (3번의 yes 확인 필요)
outo-models reset
```

자세한 내용은 [docs/index.md](docs/index.md) 를 참고하세요.

## 개발

```bash
uv sync
make lint typecheck test
```

개발 규칙은 [AGENTS.md](AGENTS.md) 를 반드시 읽고 따르세요.
