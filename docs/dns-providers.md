# DNS 제공자

`outo_models.dns` 패키지는 설정 마법사와 Caddy 가 공통으로 사용하는 비동기
인터페이스 (`DNSProvider` ABC) 를 정의합니다. 구체 구현 (Cloudflare, 수동) 은
이 인터페이스를 구현하고 [factory.create_provider](../src/outo_models/dns/factory.py)
에서 디스패치됩니다. 새 제공자를 추가하는 4단계 절차는
[src/outo_models/dns/README.md](../src/outo_models/dns/README.md) 에 있습니다.

이 문서는 운영자가 마법사에서 어떤 제공자를 골라야 하는지, 토큰은 어디서
만드는지, 수동 모드는 어떻게 동작하는지를 다룹니다.

## 인터페이스

```python
@dataclass(frozen=True, slots=True)
class DnsRecord:
    name: str         # 예: models.example.com
    type: str         # "A" | "AAAA" | "CNAME" | "TXT"
    value: str        # IPv4 / IPv6 / target / 텍스트
    ttl: int = 300    # 5분 기본값 — ACME 검증 왕복을 짧게

class DNSProvider(ABC):
    name: str         # 팩토리 디스패치 키

    async def ensure_record(self, record: DnsRecord) -> None
    async def delete_record(self, record: DnsRecord) -> None
    async def list_records(self) -> list[DnsRecord]
```

`ensure_record` 는 멱등입니다 — 같은 레코드가 이미 있으면 업데이트, 없으면
생성합니다. 마법사가 재실행되어도 안전합니다.

## 오류 매핑

| 상황 | 예외 |
| --- | --- |
| 알 수 없는 제공자 이름 | `ConfigError` (`config_error`) |
| 제공자별 자격 증명 누락 | `ConfigError` |
| 제공자 4xx 응답 (인증 / 검증 / not-found) | `ConfigError` + 제공자 메시지 |
| 제공자 5xx / 네트워크 오류 | `OutoError(code="dns_upstream")` |

`dns_upstream` 은 일시적 오류로 간주되어 마법사가 재시도 안내를 표시합니다.

## Cloudflare 모드

대부분의 운영자에게 권장되는 모드입니다. Cloudflare DNS 가 위임된 도메인에
대해 `A` 레코드를 자동으로 만들고, 이후 ACME 의 DNS-01 챌린지도 같은 토큰으로
처리합니다.

### API 토큰 발급

1. <https://dash.cloudflare.com/profile/api-tokens> 에서 **Create Token**
2. 템플릿 선택: **Edit zone DNS** (또는 **Custom token**)
3. 권한 (Permissions):
   - `Zone` → `DNS` → `Edit`
   - `Zone Resources` → `Include` → `Specific zone` → `<your-zone>` (예: `example.com`)
4. **Continue to summary** → **Create Token**
5. 생성된 토큰을 마법사에 붙여 넣기 (또는 `OUTO_CLOUDFLARE_API_TOKEN` 으로 주입)

> 토큰은 **Zone.DNS:Edit** 권한만 있으면 됩니다. 더 넓은 권한 (예: Zone Read,
> Account Read) 은 부여하지 마세요.

### 마법사 응답

`--dns-provider cloudflare` 로 마법사를 실행하면 다음이 자동으로 일어납니다.

1. Cloudflare API 로 `GET /zones?name=<domain>` 호출 → zone_id 캐시
2. `POST /zones/{zone_id}/dns_records` (또는 이미 있으면 `PUT`) 로
   `{ name: <domain>, type: A, content: <ipv4>, ttl: 300 }` 레코드 보장
3. 마법사 진행 → Caddy 가 같은 토큰으로 DNS-01 ACME 챌린지 수행

API 응답 본문에 토큰이 포함될 경우 마법사는 `re.sub(r"[A-Za-z0-9_-]{32,}",
"***", ...)` 마스킹을 거쳐 한국어 메시지를 만듭니다.

### 마스킹

`CloudflareProvider.__repr__` 은 zone domain 만 표시합니다 (`api_token` 절대
포함 안 함). 로그 / 예외 메시지 / DB 어디에도 평문이 새지 않습니다.

## 수동 모드

Cloudflare 외 DNS 호스트 (Route53, 가디라이브, ...), 또는 마법사가 DNS 를
건드리지 못하게 막아 둔 환경에서 사용합니다.

### 마법사 응답

`--dns-provider manual` 로 실행하면 다음이 자동으로 일어납니다.

1. `ManualProvider._pending` 메모리 dict 에 `{name, type, value, ttl}` 저장
2. `ManualProvider.instructions()` 가 한국어 안내를 stdout 으로 출력
3. 운영자가 안내에 따라 DNS 호스트 측에서 직접 레코드 생성
4. 마법사가 `prompts.confirm("DNS 레코드가 전파되었으면 Enter 키를 눌러 주세요.", default=True)` 로 사용자 확인 대기

예시 안내:

```
다음 DNS 레코드를 example.com 의 DNS 호스트에 추가하세요:

1. 이름(name): models.example.com  유형(type): A  값(value): 203.0.113.10  TTL: 300s

레코드가 전파된 것을 확인한 뒤 설치 마법사에서 '확인'을 눌러 주세요.
```

전파 확인 (예: `dig +short models.example.com @1.1.1.1`) 후 마법사를 진행하면
됩니다. 메모리 dict 는 마법사 인스턴스가 끝나면 사라지므로, 재호출하면 같은
안내가 다시 출력됩니다.

### DNS-01 챌린지를 수동으로 처리하고 싶다면

수동 모드에서는 Cloudflare 플러그인이 없으므로 Caddy 는 HTTP-01 챌린지를
사용합니다. 즉, ACME 발급 시점에 80 포트가 외부에서 도달 가능해야 합니다
(보통의 운영 환경이면 충족). 도메인 wildcard 인증서나 비공개 도메인 인증서가
필요하다면 [troubleshooting.md](troubleshooting.md) 의 ACME 섹션을 보세요.

## 새 제공자 추가 (4 단계)

[src/outo_models/dns/README.md](../src/outo_models/dns/README.md) 에 정식 절차가
있습니다. 요약:

1. **ABC 구현**: `src/outo_models/dns/<name>.py` 에 `DNSProvider` 서브 클래스.
   `__aenter__` / `__aexit__` 로 httpx / boto3 같은 클라이언트를 관리.
2. **오류 매핑**: 4xx → `ConfigError`, 5xx / 네트워크 → `OutoError(code="dns_upstream")`.
   예외 메시지에 자격 증명 포함 금지.
3. **팩토리 등록**: `dns/factory.create_provider` 에 새 분기 추가. 자격 증명
   누락은 `ConfigError` 로 즉시 거절.
4. **내보내기 / 테스트**: `dns/__init__.py` 에서 export. `tests/unit/test_dns_<name>.py`
   에 성공 / 갱신 / 오류 / 시크릿 위생 / (HTTP 기반이면) zone 캐싱 테스트 추가.
   `tests/unit/test_dns_factory.py` 의 디스패치 케이스도 추가.

마법사 / TLS 매니저는 `DNSProvider` 인터페이스만 보기 때문에 새 제공자가
들어와도 다른 모듈을 수정할 필요가 없습니다.

## 운영 체크리스트

DNS 모드를 변경하려면:

1. `/etc/outo-models/config.yaml` 의 `dns_provider` 키 변경
2. (Cloudflare 로 전환 시) Cloudflare API 토큰을 `cloudflare_api_token` 키로 추가
3. `outo-models setup` 을 다시 실행 (멱등) — DNS / Caddyfile 단계가 새 설정으로 재생성됨
4. `outo-models restart` — 새 Caddyfile 적용

## 다음 단계

- [setup-wizard.md](setup-wizard.md) — DNS 단계의 프롬프트 순서
- [security.md](security.md) — DNS 토큰의 시크릿 위생
- [troubleshooting.md](troubleshooting.md) — DNS 전파 / 인증서 발급 실패 디버깅
