"""CLI → 서버 관리 REST 클라이언트.

`outo-models admin --remote ...` 커맨드가 이 패키지를 통해 실행 중인
서버의 `/api/admin/*` 엔드포인트와 통신합니다.

Public surface:
    * `AdminApiClient` — bearer PAT 인증을 사용하는 동기 httpx 클라이언트.
    * `AdminApiError` — 모든 전송 실패를 통합하는 typed 예외.

세부 구현은 `api.py`를 참고하세요.
"""

from outo_models.cli_remote.api import AdminApiClient, AdminApiError

__all__ = ["AdminApiClient", "AdminApiError"]
