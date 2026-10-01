# Azure Cost Python

Azure Cost Management Query API 를 호출해 구독 비용을 CSV 로 저장합니다. Azure CLI,
Azure SDK, 외부 Python 패키지 없이 Python 3.13 표준 라이브러리만 사용합니다.

## 주요 기능

- 구독별 `ActualCost` 조회
- 여러 구독과 필터 조합을 하나의 CSV 로 저장
- API 페이지네이션 처리
- 429 및 일시적인 HTTP 오류 재시도
- Access Token, Workload Identity, Managed Identity 인증
- Resource Group, Resource Type, Resource ID prefix 필터
- UTF-8 BOM 이 포함된 CSV 생성
- 임시 CSV 생성 후 최종 경로에 저장

## 파일 구성

| 파일 | 역할 |
| --- | --- |
| `run.py` | 소스 체크아웃에서 패키지를 실행하는 진입점 |
| `src/azure_cost_management/main.py` | API 요청, 재시도, 페이지네이션, 전체 실행 흐름 |
| `src/azure_cost_management/azure_auth.py` | ARM Access Token 발급 및 캐시 |
| `src/azure_cost_management/config.py` | TOML 설정 로드 및 검증 |
| `src/azure_cost_management/cost_generate.py` | 출력 스키마와 임시 CSV 생성 |
| `src/azure_cost_management/cost_save.py` | 임시 CSV의 최종 경로 저장 |
| `config.example.toml` | 실행 설정 예시 |

## 요구 사항

- Python 3.13
- Azure Cost Management Query API 호출 권한
- `management.azure.com` 과 선택한 인증 endpoint 에 대한 네트워크 연결

런타임 의존성 설치는 필요하지 않습니다. `pyproject.toml`에는 Ruff 와 basedpyright 의
개발 도구 설정만 포함되어 있습니다.

## 설정

`config.example.toml`을 기준으로 `config.toml`을 작성합니다.

```bash
cp config.example.toml config.toml
```

```toml
# 생략하면 실행일 기준 7일 전 하루를 조회합니다.
# YYYYMMDD 또는 YYYY-MM-DD 형식을 지원합니다.
# start_date = "20250601"
# end_date = "20260531"

[[targets]]
subscription_id = "00000000-0000-0000-0000-000000000000"
subscription_name = "Sandbox Nonprod"
resource_groups = ["rg-example"]
resource_types = ["microsoft.storage/storageaccounts"]
resource_id_prefixes = ["/subscriptions/.../providers/..."]

[generate]
type = "csv"
# 선택: 임시 CSV 작성 경로
temp_dir = "C:/tmp/azure-cost"

[save]
output_dir = "./raw-cost"

```

`start_date` / `end_date` 규칙:

- 함께 생략하면 실행일 기준 7일 전 하루를 조회
- 포맷: `YYYYMMDD` 또는 `YYYY-MM-DD` (내부적으로 `YYYYMMDD`로 정규화, 파일명도 `YYYYMMDD`)
- 조회 범위: `start_date` 00:00(UTC) ~ **`end_date` 당일 포함**
- `granularity=Daily` → 응답은 `UsageDate` 일 단위 행

### 날짜 우선순위

조회 기간은 다음 순서로 결정됩니다.

1. 명령행 `--start-date` / `--end-date` (실행 시 전달)
2. 설정 파일의 `start_date` / `end_date`
3. 기본값: 실행일 기준 7일 전 하루

셸이나 Python 에서 상대 날짜를 계산해 명령행 인자로 전달할 수 있습니다.

```bash
# 현재 날짜 기준 15일 전부터 오늘까지 조회
python run.py --config config.toml \
  --start-date "$(date -d '15 days ago' +%Y%m%d)" \
  --end-date "$(date +%Y%m%d)"
```

### 조회 대상

`[[targets]]`를 반복해 여러 조회 조건을 지정합니다.

| 항목 | 필수 | 설명 |
| --- | --- | --- |
| `subscription_id` | 예 | 조회 대상 Azure 구독 ID |
| `subscription_name` | 예 | CSV 의 `SubscriptionName` 컬럼 값 |
| `resource_groups` | 아니요 | API 에 적용할 Resource Group 목록 |
| `resource_types` | 아니요 | API 에 적용할 Resource Type 목록 |
| `resource_id_prefixes` | 아니요 | API 응답에 적용할 Resource ID prefix 목록 |

필터 규칙:

- `resource_groups`와 `resource_types`를 함께 지정하면 두 조건을 모두 만족하는 비용 조회
- `resource_id_prefixes`는 API 응답을 받은 후 적용 (API 미지원 연산)
- prefix 비교는 대소문자를 구분하지 않으며, prefix 자체와 그 하위 리소스를 포함
- 서로 겹치는 조회 조건은 동일한 비용 행을 중복으로 생성할 수 있음

### 출력

모든 대상의 결과는 다음 파일 하나에 저장됩니다.

```text
<output_dir>/<file_prefix>-YYYYMMDD-YYYYMMDD.csv
```

`file_prefix`는 `[generate]` 설정으로 지정하며 기본값은 `_raw-cost`입니다.
`start_date`와 `end_date`가 같은 단일일 조회의 경우 날짜를 하나만 표기합니다.

```text
<output_dir>/<file_prefix>-YYYYMMDD.csv
```

API 응답 컬럼 뒤에 다음 추가 컬럼을 붙입니다.

| 컬럼 | 값 |
| --- | --- |
| `ResourceName` | `ResourceId`를 `/`로 나눈 뒤 마지막 두 값을 `/`로 연결한 값 |
| `SubscriptionId` | 조회 대상의 `subscription_id` |
| `SubscriptionName` | 조회 대상의 `subscription_name` |

`ResourceName` 변환 예시는 다음과 같습니다.

```text
/subscriptions/id/resourceGroups/rg/providers/microsoft.keyvault/vaults/example-vault
-> vaults/example-vault
```

출력 규칙:

- 대상 설정 순서 유지
- API 페이지 순서 유지
- API 응답 행 순서 유지
- 별도 정렬 없음

경로 규칙:

- `output_dir`, `temp_dir`의 상대 경로는 현재 작업 디렉터리가 아닌 설정 파일 위치 기준
- 같은 기간의 파일이 있으면 새 CSV 작성이 끝난 후 교체

`temp_dir` 동작:

- 지정 시 CSV 를 해당 경로에 작성한 뒤 완료 시점에 한 번만 `output_dir`로 이동
- 미지정 시 시스템 임시 경로 사용
- 같은 파일시스템이면 원자적 교체, 다른 파일시스템(Azure Blob 마운트 등)이면 복사 후 삭제
- 네트워크 저장소를 `output_dir`로 쓸 때 임시 파일을 로컬 디스크(`/tmp`, `C:/tmp` 등)에 두어 증분 트래픽 방지

## 인증

인증 정보는 설정 파일이 아닌 환경 변수로 전달합니다.

| `AZURE_AUTH_MODE` | 필수 환경변수 | 설명 |
| --- | --- | --- |
| `access-token` | `AZURE_ACCESS_TOKEN_FILE` 또는 `AZURE_ACCESS_TOKEN` | 사전 발급 토큰 사용 |
| `workload-identity` | `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_FEDERATED_TOKEN_FILE` | Kubernetes ServiceAccount 토큰 교환 |
| `managed-identity` | 없음 | Azure Managed Identity endpoint 사용 |
| `auto` | 선택한 방식에 따라 결정 | 사용 가능한 환경변수로 인증 방식 선택 |

인증 규칙:

- `auto`는 Access Token → Workload Identity → Managed Identity 순서로 선택
- Managed Identity 자동 선택에는 `IDENTITY_ENDPOINT` 또는 `MSI_ENDPOINT` 필요
- 해당 환경 변수가 없는 VM/VMSS 에서 IMDS 를 쓰려면 `AZURE_AUTH_MODE=managed-identity` 명시
- `AZURE_ACCESS_TOKEN_FILE`과 `AZURE_ACCESS_TOKEN`을 함께 지정하면 파일 우선
- 사전 발급 토큰은 자동 갱신하지 않음, Workload/Managed Identity 토큰은 만료 전 재발급
- 사용자 할당 Managed Identity 는 `AZURE_CLIENT_ID`로 선택

방식별 흐름:

- AKS Workload Identity: Entra ID endpoint 에 ServiceAccount Token 을 제출해 ARM 토큰 발급 (Managed Identity endpoint 미사용)
- AKS Pod Identity / Azure 리소스 Managed Identity: Azure Managed Identity endpoint 에 토큰 요청 (`AUTHORITY_HOST` 직접 호출 없음, `MANAGEMENT_RESOURCE`를 Resource 로 사용)

## 실행

`run.py`가 `src` 디렉터리를 자동으로 Python 경로에 추가하므로 별도 설정 없이
실행합니다.

```bash
python run.py --config config.toml
```

조회 기간을 명령행 인자로 지정할 수 있습니다 (설정 파일의 값보다 우선).

```bash
python run.py --config config.toml --start-date 20250601 --end-date 20260531
```

셸이나 Python 에서 상대 날짜를 계산해 전달합니다.

```bash
# 현재 날짜 기준 15일 전부터 오늘까지 조회
python run.py --config config.toml \
  --start-date "$(date -d '15 days ago' +%Y%m%d)" \
  --end-date "$(date +%Y%m%d)"
```

Windows PowerShell:

```powershell
$start = (Get-Date (Get-Date).AddDays(-15) -Format yyyyMMdd)
python run.py --config config.toml --start-date $start --end-date (Get-Date -Format yyyyMMdd)
```

설정 파일 경로는 환경변수로도 전달할 수 있습니다.

```bash
export AZURE_COST_CONFIG=/app/config/config.toml
python run.py
```

실행 로그에는 인증 방식, 조회 대상, 페이지, 출력 경로, 행 수, `CostUSD` 합계가
기록됩니다. 설정/인증/API/저장 오류 발생 시 종료 코드 `1`을 반환합니다.

## HTTP 재시도

- 재시도 대상: `429`, `500`, `502`, `503`, `504` 응답과 일시적인 네트워크 오류
- 대기 시간: `Retry-After` 헤더 우선, 없으면 `attempt * HTTP_RETRY_WAIT_SECONDS`
- 갱신 가능한 인증 방식에서 `401` 발생 시 토큰 재발급 후 재시도
- `HTTP_MAX_ATTEMPTS`에는 최초 요청이 포함
