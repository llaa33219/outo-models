#!/usr/bin/env bash
# outo-models 방화벽 포트 개방 스크립트
#
# CLI가 컨테이너에서 호스트 측으로 호출합니다. sudo + 이 스크립트 경로로
# 실행되는 것을 전제로 합니다. 권한 부족 / 누락된 도구는 호출 측(setup wizard)이
# 사전에 검증하지만, 이 스크립트는 가능한 한 멱등(idempotent)으로 동작합니다 —
# 같은 인자로 여러 번 호출해도 에러를 내지 않습니다.
#
# 사용법:
#   firewall-open.sh <kind> <port...>
#
# kind:
#   firewalld  : firewalld 영구 규칙 + reload
#   ufw        : ufw allow
#   nftables   : outo_models 전용 테이블/체인에 dport 규칙 추가
#   none       : 방화벽 미감지 — 한국어 안내만 출력하고 0으로 종료
#
# 종료 코드:
#   0  : 성공 (또는 변경 없음)
#   64 : 사용법 오류 (EX_USAGE)
#   그 외 : 도구 실행 실패

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <kind> <port...>" >&2
    exit 64
fi

kind=$1
shift

if [[ $# -eq 0 ]]; then
    echo "at least one port required" >&2
    exit 64
fi

# 인자로 들어온 값은 신뢰 가능한 argv이지만, 방어적으로 한 번 더 정수만 받게 검증.
for p in "$@"; do
    if ! [[ $p =~ ^[0-9]+$ ]] || (( p < 1 || p > 65535 )); then
        echo "invalid port: ${p}" >&2
        exit 64
    fi
done

add_firewalld() {
    local p
    for p in "$@"; do
        firewall-cmd --permanent --add-port="${p}/tcp"
    done
    firewall-cmd --reload
}

add_ufw() {
    local p
    for p in "$@"; do
        ufw allow "${p}/tcp"
    done
}

add_nftables() {
    # 전용 테이블 / 체인을 만들고 거기에 누적한다. 같은 규칙이 이미 있으면 건너뛴다.
    nft add table inet outo_models 2>/dev/null || true
    nft add chain inet outo_models outo_models_input \
        '{ type filter hook input priority 0 ; policy accept ; }' 2>/dev/null || true
    for p in "$@"; do
        if nft list ruleset | grep -qE "tcp dport ${p}([[:space:]]|$)"; then
            echo "nftables: port ${p} already open"
        else
            nft add rule inet outo_models outo_models_input tcp dport "${p}" counter accept
        fi
    done
}

case "$kind" in
    firewalld)
        add_firewalld "$@"
        ;;
    ufw)
        add_ufw "$@"
        ;;
    nftables)
        add_nftables "$@"
        ;;
    none)
        cat <<'EOF'
감지된 방화벽이 없습니다. outo-models는 외부에서 접속 가능한 포트(80, 443)를
운영체제 방화벽 또는 클라우드 보안 그룹에서 직접 열어 주셔야 합니다.
EOF
        ;;
    *)
        echo "unknown firewall kind: ${kind}" >&2
        exit 64
        ;;
esac
