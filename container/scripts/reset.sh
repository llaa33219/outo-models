#!/usr/bin/env bash
# reset.sh — outo-models 컨테이너와 데이터 볼륨을 모두 삭제
#
# **호출 전 안전장치는 CLI 측 책임입니다.** 이 스크립트는 그 어떤 확인도
# 하지 않습니다 — 호출하는 쪽에서 이미 AGENTS.md §2 의 "3회 yes" 게이트를
# 통과했다고 가정합니다 (AGENTS.md §2: "reset 안전장치 불변").
#
# 동작:
#   1. outo-models 컨테이너가 있으면 stop + rm (없으면 무시, 멱등)
#   2. outo-models-data 볼륨이 있으면 rm (없으면 무시, 멱등)
#   3. 한국어 완료 메시지 출력
#
# 호스트에 podman 이 없으면 0 으로 graceful 종료합니다 — update.sh 와 동일한
# 이유. 그러나 이 스크립트는 *데이터 삭제* 라는 매우 파괴적인 동작을
# 수행하므로, 운영 환경에서 podman 부재는 명백한 설정 오류입니다. 메시지에
# 그 사실을 분명히 적어 둡니다.
#
# 사용법: reset.sh (인자 없음)
# 종료 코드: 0 (성공 / 멱등 no-op / podman 부재)
#           1 (실제 삭제 실패 — 보통 권한 문제)

set -euo pipefail

if [[ $# -ne 0 ]]; then
    echo "사용법: $0  (인자 없음)" >&2
    exit 64
fi

container_name="outo-models"
volume_name="outo-models-data"

# -----------------------------------------------------------------------------
# podman 존재 검사
# -----------------------------------------------------------------------------
if ! command -v podman >/dev/null 2>&1; then
    cat >&2 <<'EOF'
[오류] 호스트에 podman 이 설치되어 있지 않습니다.

  reset.sh 는 컨테이너와 데이터 볼륨을 *삭제* 하는 매우 파괴적인 동작을
  수행합니다. podman 이 없으면 어떤 삭제도 일어나지 않으며, 그 사실이
  호출자에게 분명히 전달되어야 합니다.

  컨테이너 배포 환경이라면 podman 을 설치한 뒤 다시 실행하세요.
EOF
    exit 1
fi

# -----------------------------------------------------------------------------
# 1) 컨테이너 정지 + 제거 (있을 때만)
# -----------------------------------------------------------------------------
if podman container exists "${container_name}"; then
    echo "[1/3] 컨테이너 정지: ${container_name}"
    # stop 실패(예: 이미 정지됨) 도 그대로 진행 — 멱등이 목적.
    podman stop "${container_name}" >/dev/null 2>&1 || true
    echo "[2/3] 컨테이너 제거: ${container_name}"
    podman rm "${container_name}"
else
    echo "[1/3] ${container_name} 컨테이너가 없습니다 — 건너뜁니다."
    echo "[2/3] (생략) 컨테이너가 없으므로 제거 단계도 건너뜁니다."
fi

# -----------------------------------------------------------------------------
# 2) 데이터 볼륨 제거 (있을 때만)
# -----------------------------------------------------------------------------
if podman volume exists "${volume_name}"; then
    echo "[3/3] 데이터 볼륨 제거: ${volume_name}"
    podman volume rm "${volume_name}"
    echo "      모든 git 저장소, SQLite DB, Caddy 인증서 상태가 삭제됩니다."
else
    echo "[3/3] ${volume_name} 볼륨이 없습니다 — 건너뜁니다."
fi

cat <<'EOF'
[완료] outo-models 가 초기 설치 상태로 되돌아갔습니다.
       다음 실행은 setup 위저드부터 다시 시작해야 합니다:
         outo-models setup
EOF
