#!/usr/bin/env bash
# outo-models 컨테이너 엔트리포인트.
#
# 책임 범위 (그 이상은 하지 않는다):
#   1. 한국어 시작 배너 + 버전 출력
#   2. AGENTS.md §4: IMAGE_FLAVOR=dev + OUTO_ENV=production 조합 거부
#   3. 비특권(non-root) 실행에서 80/443 바인딩 가능 여부 사전 경고
#   4. /etc/outo-models/config.yaml 존재 여부 안내 (필수는 아님)
#   5. `outo-models "$@"` 으로 exec — 셸을 교체해 시그널이 CLI 에게 직접 전달
#      되도록 한다 (SIGTERM/SIGINT 가 uvicorn 으로 곧장 전달됨).
#
# 환경변수:
#   IMAGE_FLAVOR  빌드 ARG 로 주입됨 (Containerfile ARG IMAGE_FLAVOR).
#                 stable | dev 만 유효; 다른 값은 빌드 단계에서 거부됨.
#   OUTO_ENV      Settings.env 와 동일. development | production.
#   OUTO_DATA_DIR 데이터 디렉터리 (기본 /var/lib/outo-models).
#   OUTO_CONFIG   설정 파일 경로 (기본 /etc/outo-models/config.yaml).
#
# 종료 코드:
#   0   정상 exec
#   1   잘못된 IMAGE_FLAVOR+OUTO_ENV 조합 / outo-models 콘솔 스크립트 누락
#   그 외 docker/podman 종료 코드

set -euo pipefail

# -----------------------------------------------------------------------------
# 시작 배너 + 버전
# -----------------------------------------------------------------------------
# venv 의 python 이 PATH 에 포함되어 있다 (Containerfile runtime-base 에서
# ENV PATH="/app/.venv/bin:/usr/local/bin:$PATH").
version=$(python -c "from outo_models.version import __version__; print(__version__)" 2>/dev/null || echo "unknown")

cat <<EOF
================================================================================
  outo-models v${version}
  IMAGE_FLAVOR=${IMAGE_FLAVOR:-stable}    OUTO_ENV=${OUTO_ENV:-production}
  DATA_DIR=${OUTO_DATA_DIR:-/var/lib/outo-models}
================================================================================
EOF

# -----------------------------------------------------------------------------
# AGENTS.md §4: dev 이미지 + production 환경 거부
# -----------------------------------------------------------------------------
if [[ "${IMAGE_FLAVOR:-stable}" == "dev" && "${OUTO_ENV:-production}" == "production" ]]; then
    cat >&2 <<EOF
[치명] dev 이미지를 production 환경 변수와 함께 실행하려고 합니다.

  IMAGE_FLAVOR=dev
  OUTO_ENV=production

이 조합은 AGENTS.md §4 에 의해 금지됩니다. dev 이미지는 debugpy / ipython 을
포함하고 있어 프로덕션에 배포해서는 안 됩니다.

다음 중 하나를 선택하세요:
  - IMAGE_FLAVOR=stable 이미지를 사용
  - OUTO_ENV=development 로 실행 (개발 머신에서만)
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# 비특권 80/443 바인딩 가능 여부 사전 경고 (실패는 하지 않음)
# -----------------------------------------------------------------------------
# Containerfile 의 EXPOSE 80 443 와 짝을 이루는 안내. Caddy 가 런타임에
# 실제로 바인딩을 시도하면서 EPERM 으로 실패할 때, 운영자가 어디를 봐야
# 하는지 이 메시지가 가리킨다.
if [[ "$(id -u)" != "0" ]]; then
    # ip_unprivileged_port_start 가 80 이하이면 비특권도 80 을 쓸 수 있다
    # (예: 일부 호스트가 0 으로 설정해 모든 포트를 비특권에게 허용).
    port_start=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || echo "32768")
    if (( port_start > 80 )); then
        cat <<'EOF'
[경고] 컨테이너를 비특권 사용자(uid=1000)로 실행 중이며 커널이 비특권에게
       80/443 포트를 허용하지 않습니다 (net.ipv4.ip_unprivileged_port_start 가
       80 초과). Caddy 가 시작하면서 권한 오류로 실패할 가능성이 높습니다.

       다음 중 하나로 해결하세요:
         1) podman run --cap-add NET_BIND_SERVICE ...   # 권장
         2) 호스트 포트 리매핑: -p 8080:80 -p 8443:443  # TLS 종료는 별도 처리 필요
       자세한 내용: docs/troubleshooting.md
EOF
    fi
fi

# -----------------------------------------------------------------------------
# /etc/outo-models/config.yaml 존재 안내 (필수가 아님 — 환경변수로도 가능)
# -----------------------------------------------------------------------------
config_path="${OUTO_CONFIG:-/etc/outo-models/config.yaml}"
if [[ ! -f "${config_path}" ]]; then
    echo "[안내] ${config_path} 이(가) 없습니다. 환경 변수로 설정하거나"
    echo "       setup 위저드를 실행해 생성하세요."
fi

# -----------------------------------------------------------------------------
# outo-models 콘솔 스크립트 존재 확인
# -----------------------------------------------------------------------------
if ! command -v outo-models >/dev/null 2>&1; then
    cat >&2 <<EOF
[치명] outo-models 콘솔 스크립트를 찾을 수 없습니다.

  PATH=${PATH}

이미지 빌드 과정에서 pyproject.toml 의 [project.scripts] 가 venv 에 설치되어야
합니다. venv 가 손상되었거나 src 가 누락된 채 빌드된 것 같습니다.
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# exec — 셸을 outo-models 로 교체해 시그널이 곧장 전달되도록 한다.
# CMD 가 ["serve"] 이며, 사용자가 podman run ... <image> migrate 처럼
# 다른 서브커맨드로 덮어쓸 수 있도록 "$@" 로 그대로 넘긴다.
# -----------------------------------------------------------------------------
exec outo-models "$@"
