# 문제 해결

운영 중 흔히 부딪히는 상황을 정리합니다. 메시지의 출처는 코드에 직접 있으니
원인을 추적할 때 [src/outo_models](../src/outo_models) 의 해당 모듈도 함께
보세요.

## 1. 방화벽

### "감지된 방화벽이 없습니다"

`firewalld` / `ufw` / `nft` 어느 것도 설치되지 않은 호스트입니다.
`firewall.open_ports.detect_firewall` 가 `FirewallKind.NONE` 을 반환하고,
`firewall-open.sh` 는 한국어 안내를 stdout 으로 출력합니다.

```
감지된 방화벽이 없습니다. outo-models는 외부에서 접속 가능한 포트(80, 443)를
운영체제 방화벽 또는 클라우드 보안 그룹에서 직접 열어 주셔야 합니다.
```

운영자가 직접 80 / 443 을 열어 주세요. `setup` 단계에서는 `--skip-firewall` 로
방화벽 단계를 건너뛰고, 운영 환경 / 클라우드 콘솔에서 인바운드 규칙을
추가하면 됩니다.

### "방화벽 명령에 권한이 없습니다"

`outo-models setup` (또는 동등한 `open_ports` 호출) 이 `sudo -n` 으로
호스트 스크립트를 실행하다가 실패했습니다 (`OutoError(code="firewall_permission")`).
마법사는 이를 한국어 `ConfigError` 로 변환해 stderr 로 출력합니다.

해결 방법:

1. `root` 권한으로 setup 을 다시 실행하거나,
2. `/etc/sudoers.d/outo-models` 에 NOPASSWD 규칙 추가:

```
<your-username> ALL=(root) NOPASSWD: /opt/outo-models/scripts/firewall-open.sh *
```

`firewall-open.sh` 는 `set -euo pipefail` 로 동작하며, 인자로 받은 `<kind>
<port...>` 만 실행하므로 안전합니다.

### nftables 규칙 충돌

`firewall-open.sh` 는 자체 테이블 `inet outo_models` 에 규칙을 누적합니다
(중복 시 skip). 같은 테이블을 다른 도구와 공유하면 안 됩니다. 충돌 시 다음
명령으로 정리합니다.

```bash
sudo nft delete table inet outo_models
```

## 2. 80 / 443 포트 바인딩 실패

비특권 실행 시 80 / 443 바인딩은 `NET_BIND_SERVICE` capability 가 필요합니다.
엔트리포인트가 컨테이너 안에서 다음과 같이 사전 경고합니다
([container/rootfs/usr/local/bin/outo-entrypoint.sh](../container/rootfs/usr/local/bin/outo-entrypoint.sh)).

```
[경고] 컨테이너를 비특권 사용자(uid=1000)로 실행 중이며 커널이 비특권에게
       80/443 포트를 허용하지 않습니다 (net.ipv4.ip_unprivileged_port_start 가
       80 초과). Caddy 가 시작하면서 권한 오류로 실패할 가능성이 높습니다.

       다음 중 하나로 해결하세요:
         1) podman run --cap-add NET_BIND_SERVICE ...   # 권장
         2) 호스트 포트 리매핑: -p 8080:80 -p 8443:443  # TLS 종료는 별도 처리 필요
```

### 권장 해결

`outo-models start` 는 항상 `--cap-add NET_BIND_SERVICE` 를 부착하므로
대부분의 경우 자동 해결됩니다. 컨테이너를 직접 띄울 때도 같은 옵션을
추가해 주세요.

### 호스트 측 sysctl 조정

`/proc/sys/net/ipv4/ip_unprivileged_port_start` 가 `0` 이면 비특권도 80 이하
포트에 바인딩 가능하지만, 보안상 권장되지 않습니다.

```bash
# 임시
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=0

# 영구
echo 'net.ipv4.ip_unprivileged_port_start=0' | sudo tee /etc/sysctl.d/99-unprivileged-ports.conf
```

### 호스트 포트 리매핑

80 / 443 을 다른 포트로 매핑하면 외부에서 표준 HTTPS 로 접속할 수 없습니다.
**테스트 / 데모 용도 외에는 권장하지 않습니다.** 매핑 예시는 다음과
같습니다.

```bash
-p 8080:80 -p 8443:443
```

이 경우 Caddy 가 8080 으로 ACME HTTP-01 챌린지를 받을 수 없으므로 DNS-01
(Cloudflare 모드) 만 사용 가능합니다.

## 4. ACME 인증서 발급 / 갱신

### "Let's Encrypt rate limit reached"

도메인 오타로 잘못된 도메인에 대해 여러 번 발급을 시도하면 Let's Encrypt 측
rate limit 에 걸립니다. 일시적으로 스테이징 CA 로 전환해 디버깅합니다.

`TlsConfig.staging = True` 로 Caddyfile 을 다시 렌더링합니다. 현재 코드에는
`outo-models setup` 의 staging 직접 토글 플래그는 노출되어 있지 않으므로,
`/etc/outo-models/Caddyfile` 에서 `acme_ca
https://acme-staging-v02.api.letsencrypt.org/directory` 라인을 추가하고
Caddy 를 재시작하세요.

### DNS-01 챌린지 실패

Cloudflare 모드에서 `CLOUDFLARE_API_TOKEN` 이 없거나 권한이 부족하면 Caddy
가 stderr 에 토큰 관련 에러를 출력합니다. 토큰이 다음을 만족하는지 확인하세요.

- `Zone.DNS:Edit` 권한
- 토큰의 `Zone Resources` 가 정확한 zone 만 포함 (모든 zone 이 아니어야)
- 만료되지 않음

### 인증서 만료 30일 전 경고

`cert_renewal_job` 이 매일 00:00 UTC 에 인증서를 점검합니다. 비정상이면
Caddy 에 reload 를 nudge 합니다. 그래도 갱신이 안 되면:

1. `journalctl -u outo-models` 에서 Caddy 로그 확인
2. `curl -v https://<domain>/` 로 외부에서 도달 가능한지 확인
3. `podman exec outo-models caddy version` 으로 Caddy 버전 확인

## 5. "unable to open database file"

DB 파일이 있는 디렉터리에 컨테이너의 비특권 사용자(uid 1000) 가 읽기 /
쓰기 권한이 없을 때 발생합니다. 다음을 확인하세요.

```bash
ls -ld /var/lib/outo-models
# drwxr-xr-x 1000 1000 ...
```

소유자가 다른 경우 (예: 컨테이너 이전에 다른 컨테이너가 사용):
```bash
sudo chown -R 1000:1000 /var/lib/outo-models
```

`setup` 이 처음 만든 디렉터리는 자동으로 권한이 잡혀 있지만, 호스트에서
다른 도구가 변경했을 가능성이 있습니다.

## 6. podman 부재 (개발 머신)

개발 머신 (AGENTS.md §4) 에는 podman 이 없습니다. `start` / `stop` /
`restart` / `update` / `reset` 은 한국어 안내와 함께 exit 1 로 종료됩니다.

```
오류 (config_error): 이 명령은 서버 호스트에서 실행되어야 합니다 (podman 미설치).
컨테이너 배포 환경의 호스트에서 다시 실행해 주세요.
```

`status` 만은 exit 0 으로 다음 메시지를 출력합니다.

```
[정보] 이 호스트에는 podman이 설치되어 있지 않습니다 (개발 환경).
```

개발 머신에서의 검증은 `uv sync` + `make lint` + `make typecheck` + `make test`
까지만 보장합니다 (자세한 내용: [testing.md](testing.md)). 이미지 동작
검증은 별도의 테스트 머신에서 진행하세요.

## 7. 로그 위치

- **컨테이너 로그**: `podman logs outo-models` (stdout/stderr 통합)
- **호스트 측 스크립트 로그**: `firewall-open.sh` 는 stdout, `update.sh` /
  `reset.sh` 는 stdout 으로 출력 — `podman run --log-driver journald ...`
  옵션을 쓰면 journald 로도 보낼 수 있음
- **Caddy 액세스 로그**: `/var/lib/outo-models/certs/` 옆의 Caddy 내부 로그
  (자세한 위치는 `podman exec outo-models caddy fmt --help` 참고)
- **DB 감사 로그**: `audit_logs` 테이블 — [`outo-models admin list` 는
  표시하지 않음, API `GET /api/admin/audit` 로 조회](admin.md#감사-로그-조회)

## 8. 컨테이너가 시작 후 곧 종료

컨테이너가 `podman run` 직후 exited 상태가 되면 다음을 확인하세요.

```bash
podman logs outo-models
```

흔한 원인:

1. **엔트리포인트의 `dev + production` 조합 거부** — `IMAGE_FLAVOR=dev` +
   `OUTO_ENV=production` 으로 띄우면 exit 1. `stable` 이미지로 바꾸거나
   `OUTO_ENV=development` 로 변경
2. **`outo-models` 콘솔 스크립트 누락** — 빌드 과정에서 venv 가 손상된 경우.
   `podman exec outo-models which outo-models` 로 PATH 확인
3. **`/etc/outo-models/config.yaml` 형식 오류** — `setup` 을 다시 실행해
   멱등으로 재생성
4. **포트 80 / 443 충돌** — 호스트의 다른 웹 서버 (nginx / apache) 가 이미
   점유 중. `sudo ss -lntp | grep -E ':80|:443'` 로 확인 후 정지

## 9. git 작업이 갑자기 401 을 반환

- PAT 이 만료되었는지 확인 (`GET /api/auth/tokens` 에서 `expires_at` 확인)
- PAT 이 revoke 되었는지 확인 (관리자가 명시적으로 폐기했거나, 사용자가
  UI 에서 폐기 버튼을 눌렀을 수 있음)
- 사용자가 `banned` 또는 `denied` 상태인지 확인 — 그런 경우 401 이 아니라
  403 이 와야 하는데, Basic auth 의 경우 401 도 가능 (`git_smart.auth` 가
  `user.is_active == False` 일 때 `ForbiddenError` 를 던지지만, 위 라우터가
  `authorize` 에서 401 로 보낼 수 있음)

## 10. `reset` 게이트가 의도치 않게 통과

3회 `yes` 게이트 (AGENTS.md §2.2) 는 다음을 만족해야만 실제 삭제로 들어갑니다.

- `--destroy` 가 CLI 인수로 명시
- `OUTO_DESTRUCTIVE=1` 환경 변수
- 세 번 연속 정확히 `yes` 입력 (대소문자 / 공백 / 다른 답 모두 거부)

스크립트에서 우회하려면 `--destroy` 와 환경 변수만 채우면 됩니다. 비대화형
스크립트로 reset 을 자동화할 수 없습니다 — 이는 의도된 동작입니다.

만약 "건드리지 말아야 할 데이터가 이미 삭제됐다" 면 데이터는 사라진
것입니다 (스냅샷 / 백업 정책이 없음). 향후 운영 시 다음과 같은 백업 정책을
권장합니다.

- `data_dir` 전체를 일 1회 오프사이트 백업 (`podman exec outo-models sqlite3
  /var/lib/outo-models/db.sqlite3 ".backup /backup/db-$(date +%F).sqlite3"`)
- `data_dir/repos/` 는 git 저장소이므로 `git clone --mirror` 로 외부 미러 가능

## 다음 단계

- [install.md](install.md) — 첫 설치 절차
- [setup-wizard.md](setup-wizard.md) — 자동 단계의 정확한 동작
- [security.md](security.md) — 인증 / 토큰 / 감사 로그 정책
