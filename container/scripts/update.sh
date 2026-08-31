#!/usr/bin/env bash
# update.sh — outo-models 이미지 갱신 + DB 마이그레이션 + 재시작
#
# 호스트에서 `outo-models update` 가 호출합니다 (CLI 가 이 스크립트의 경로를
# 미리 알고 있다는 전제). 동작:
#   1. 인자 1 의 이미지 태그를 pull (기본 outo-models:stable)
#   2. throwaway 컨테이너에서 `outo-models migrate` 실행 (DB 마이그레이션)
#   3. 동일 이름의 컨테이너가 실행 중이면 `podman restart`
#
# 호스트에 podman 이 없는 환경에서는 0 으로 종료합니다 — 이 스크립트는
# 호스트에서만 호출되어야 하므로, 컨테이너 내부에서 실행될 일은 없습니다.
# 그래도 CI 같은 환경에서 안전하게 동작하도록 graceful 하게 처리합니다.
#
# 사용법:
#   update.sh [<image-tag>]
#
# 종료 코드:
#   0   성공 (또는 podman 부재로 인한 no-op)
#   64  사용법 오류
#   그 외 마이그레이션/pull/restart 실패 코드

set -euo pipefail

if [[ $# -gt 1 ]]; then
    echo "사용법: $0 [<image-tag>]" >&2
    exit 64
fi

image_tag="${1:-outo-models:stable}"
container_name="outo-models"
volume_name="outo-models-data"

# -----------------------------------------------------------------------------
# podman 존재 검사 — 부재 시 0 으로 graceful 종료
# -----------------------------------------------------------------------------
if ! command -v podman >/dev/null 2>&1; then
    cat <<'EOF'
[안내] 호스트에 podman 이 설치되어 있지 않습니다.

  update.sh 는 호스트 측 스크립트이므로 컨테이너 배포 환경이 아닌 경우
  동작할 필요가 없습니다. 이 메시지를 무시해도 됩니다.

  만약 컨테이너로 배포하는 환경이라면 podman 을 설치한 뒤 다시 실행하세요.
EOF
    exit 0
fi

# -----------------------------------------------------------------------------
# 1) 새 이미지 pull
# -----------------------------------------------------------------------------
echo "[1/3] 이미지 pull: ${image_tag}"
podman pull "${image_tag}"

# -----------------------------------------------------------------------------
# 2) 마이그레이션 (throwaway 컨테이너, 동일 데이터 볼륨 마운트)
# -----------------------------------------------------------------------------
# `migrate` 서브커맨드는 CLI 팀(WP-14) 이 제공합니다. 아직 구현 전이라면
# `podman run` 이 "unknown command" 로 실패하면서 set -e 가 0 이 아닌 코드를
# 반환합니다 — 그건 *정상적인* 신호이며, fix-forward 합니다.
echo "[2/3] DB 마이그레이션 실행"
podman run --rm \
    -v "${volume_name}:/var/lib/outo-models" \
    "${image_tag}" \
    outo-models migrate

# -----------------------------------------------------------------------------
# 3) 기존 컨테이너 재시작 (있을 때만)
# -----------------------------------------------------------------------------
echo "[3/3] 컨테이너 재시작 확인"
if podman container exists "${container_name}"; then
    podman restart "${container_name}"
    echo "      ${container_name} 컨테이너를 재시작했습니다."
else
    echo "      실행 중인 ${container_name} 컨테이너가 없습니다. 수동으로 시작하세요:"
    echo "        podman run -d --name ${container_name} -p 80:80 -p 443:443 \\"
    echo "          -v ${volume_name}:/var/lib/outo-models \\"
    echo "          --cap-add NET_BIND_SERVICE \\"
    echo "          ${image_tag}"
fi

cat <<'EOF'
[완료] outo-models 업데이트가 끝났습니다.
EOF
