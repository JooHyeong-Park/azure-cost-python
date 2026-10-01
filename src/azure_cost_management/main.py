#!/usr/bin/env python3
"""Azure Cost Management 비용을 조회해 기간별 단일 CSV 로 저장한다."""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from collections.abc import Iterator, Mapping, Sequence
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .azure_auth import TokenProvider, create_token_provider
from .config import (
    Settings,
    Target,
    load_settings,
)
from .cost_generate import (
    AZURE_COLUMNS,
    DIMENSION_COLUMNS,
    CostRow,
    add_custom_fields,
)
from .cost_save import save

LOGGER = logging.getLogger(__name__)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MANAGEMENT_ENDPOINT = "https://management.azure.com"


class CostError(RuntimeError):
    """비용 API 요청 또는 응답 검증 실패."""

    pass


class HttpError(CostError):
    """HTTP 상태와 응답 헤더를 포함한 비용 API 오류."""

    status: int
    headers: Mapping[str, str]

    def __init__(self, status: int, headers: Mapping[str, str], body: str) -> None:
        self.status = status
        self.headers = headers
        super().__init__(f"HTTP {status}: {body[:2000]}")


def build_query(settings: Settings, target: Target) -> dict[str, Any]:
    """조회 기간과 대상 필터로 Cost Management 요청 본문을 생성한다."""

    grouping = [
        {"type": "Dimension", "name": column} for column in DIMENSION_COLUMNS
    ]
    dataset: dict[str, Any] = {
        "granularity": "Daily",
        "aggregation": {"cost": {"name": "CostUSD", "function": "Sum"}},
        "grouping": grouping,
    }
    filters = []
    if target.resource_groups:
        filters.append(_dimension("ResourceGroup", target.resource_groups))
    if target.resource_types:
        filters.append(_dimension("ResourceType", target.resource_types))
    if len(filters) == 1:
        dataset["filter"] = filters[0]
    elif filters:
        dataset["filter"] = {"and": filters}
    return {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": _to_iso_date(settings.start_date),
            "to": _to_iso_date(settings.end_date),
        },
        "dataset": dataset,
    }


def _to_iso_date(value: str) -> str:
    """YYYYMMDD 날짜를 API 가 요구하는 ISO YYYY-MM-DD 로 변환한다."""

    return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"


def _dimension(name: str, values: Sequence[str]) -> dict[str, object]:
    """Cost Management dimension 의 In 필터를 생성한다."""

    return {"dimensions": {"name": name, "operator": "In", "values": values}}


def _request_json(request: Request, timeout: float) -> dict[str, Any] | None:
    """HTTP 요청을 실행해 JSON 객체 또는 빈 응답을 반환한다."""

    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
            if response.status == 204 or not body:
                return None
            payload = json.loads(body.decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HttpError(exc.code, dict(exc.headers.items()), body) from exc
    except (URLError, TimeoutError) as exc:
        raise CostError(f"HTTP request failed: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CostError("response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CostError("response JSON must be an object")
    return payload


def _retry_after(headers: Mapping[str, str], fallback: float) -> float:
    """Retry-After 헤더를 대기 시간(초)으로 변환한다."""

    value = next(
        (value for name, value in headers.items() if name.lower() == "retry-after"),
        None,
    )
    if value is None:
        return fallback
    # 초 단위 값 우선, HTTP-date 형식 추가 지원
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return fallback


class CostClient:
    """Cost Management API 요청, 재시도, 페이지네이션을 처리한다."""

    settings: Settings
    tokens: TokenProvider
    allowed_host: tuple[str, str]

    def __init__(self, settings: Settings, tokens: TokenProvider) -> None:
        self.settings = settings
        self.tokens = tokens
        # nextLink 검증 기준으로 ARM endpoint 의 scheme 과 host 저장
        endpoint = urlsplit(MANAGEMENT_ENDPOINT)
        self.allowed_host = (endpoint.scheme.lower(), endpoint.netloc.lower())

    def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """비용 API 를 호출하고 재시도 가능한 오류를 처리한다."""

        data = json.dumps(body, separators=(",", ":")).encode()
        refresh = False
        for attempt in range(1, self.settings.max_attempts + 1):
            request = Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {self.tokens.get_token(refresh)}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "azure-cost-python/1.0",
                },
                method="POST",
            )
            try:
                return _request_json(request, self.settings.timeout)
            except HttpError as exc:
                # 401 응답 시 갱신 가능한 토큰 재발급
                if (
                    exc.status == 401
                    and self.tokens.can_refresh
                    and attempt < self.settings.max_attempts
                ):
                    refresh = True
                    continue
                if (
                    exc.status not in RETRYABLE_STATUS_CODES
                    or attempt == self.settings.max_attempts
                ):
                    raise
                # Retry-After 미제공 시 시도 횟수에 비례해 대기
                wait = _retry_after(exc.headers, attempt * self.settings.retry_wait)
            except CostError:
                if attempt == self.settings.max_attempts:
                    raise
                wait = attempt * self.settings.retry_wait
            LOGGER.info(
                "request retry in %gs (%d/%d)",
                wait,
                attempt,
                self.settings.max_attempts,
            )
            time.sleep(wait)
        raise CostError("request attempts exhausted")

    def rows(self, target: Target, body: dict[str, Any]) -> Iterator[CostRow]:
        """전체 응답 페이지를 순회해 컬럼명 기준의 비용 행을 생성한다."""

        url = (
            f"{MANAGEMENT_ENDPOINT}/subscriptions/"
            f"{target.subscription_id}/providers/Microsoft.CostManagement/query"
            f"?api-version={self.settings.api_version}"
        )
        page = 1
        while url:
            LOGGER.info("RAW_COST_PAGE :: %d", page)
            LOGGER.info("RAW_COST_URL  :: %s", url)
            payload = self._post(url, body)
            if payload is None:
                return
            properties = payload.get("properties")
            if not isinstance(properties, dict):
                raise CostError("response has no properties object")
            columns = properties.get("columns")
            rows = properties.get("rows")
            if not isinstance(columns, list) or not isinstance(rows, list):
                raise CostError("response columns or rows are invalid")
            # 응답 컬럼 순서 변경에 대비한 이름별 index 구성
            indexes = {
                column["name"].casefold(): index
                for index, column in enumerate(columns)
                if isinstance(column, dict) and isinstance(column.get("name"), str)
            }
            missing = [name for name in AZURE_COLUMNS if name.casefold() not in indexes]
            if missing:
                raise CostError(f"response is missing columns: {', '.join(missing)}")
            for row in rows:
                if not isinstance(row, list):
                    raise CostError("response row is invalid")
                try:
                    result = {
                        name: row[indexes[name.casefold()]] for name in AZURE_COLUMNS
                    }
                except IndexError as exc:
                    raise CostError("response row is shorter than columns") from exc
                # prefix 필터는 API 미지원이라 응답 행에서 적용
                if _matches_prefix(result["ResourceId"], target.resource_id_prefixes):
                    yield result

            next_link = properties.get("nextLink")
            if not next_link:
                return
            if not isinstance(next_link, str):
                raise CostError("nextLink is invalid")
            parsed = urlsplit(next_link)
            # ARM endpoint 와 scheme 또는 host 가 다른 nextLink 거부
            if (parsed.scheme.lower(), parsed.netloc.lower()) != self.allowed_host:
                raise CostError("nextLink points to an unexpected host")
            LOGGER.info("RAW_COST_NEXT_LINK :: %s", next_link)
            url = next_link
            page += 1


def _matches_prefix(resource_id: object, prefixes: Sequence[str]) -> bool:
    """ResourceId 와 대소문자를 구분하지 않는 prefix 일치 여부를 반환한다."""

    if not prefixes:
        return True
    if not isinstance(resource_id, str):
        return False
    resource_id = resource_id.rstrip("/").casefold()
    normalized = [prefix.rstrip("/").casefold() for prefix in prefixes]
    return any(
        resource_id == prefix or resource_id.startswith(f"{prefix}/")
        for prefix in normalized
    )


def _billing_rows(
    client: CostClient, settings: Settings, targets: Sequence[Target]
) -> Iterator[CostRow]:
    """모든 대상을 순차 조회해 대상 식별 값이 포함된 비용 행을 생성한다."""

    for target in targets:
        body = build_query(settings, target)
        LOGGER.info(
            "RAW_COST_SCOPE :: %s|%s|%s|%s|%s",
            target.subscription_name,
            target.subscription_id,
            ",".join(target.resource_groups),
            ",".join(target.resource_types),
            ",".join(target.resource_id_prefixes),
        )
        for row in client.rows(target, body):
            add_custom_fields(row, target)
            yield row


def run(settings: Settings) -> None:
    """인증과 API 클라이언트를 구성해 전체 조회 결과를 저장한다."""

    mode, tokens = create_token_provider(settings.timeout)
    LOGGER.info("AUTH_MODE :: %s", mode)
    client = CostClient(settings, tokens)

    result = settings.generate.generate(
        settings.start_date,
        settings.end_date,
        _billing_rows(client, settings, settings.targets),
    )
    try:
        destination = save(result.temporary_path, result.file_name, settings.save)
    finally:
        result.temporary_path.unlink(missing_ok=True)
    LOGGER.info("WROTE :: %s", destination)
    LOGGER.info("ROWS :: %d", result.rows_written)
    LOGGER.info("TOTAL_COST_USD :: %s", result.total_cost_usd)


def main() -> int:
    """명령행 설정을 처리하고 실행 결과를 종료 코드로 반환한다."""

    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=os.getenv("AZURE_COST_CONFIG"),
        help="TOML config path; may also be set with AZURE_COST_CONFIG",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="start date as YYYYMMDD or YYYY-MM-DD; overrides config start_date",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="end date as YYYYMMDD or YYYY-MM-DD; overrides config end_date",
    )
    args = parser.parse_args()
    if args.config is None:
        parser.print_usage()
        LOGGER.error("--config or AZURE_COST_CONFIG is required")
        return 2
    try:
        run(
            load_settings(
                args.config,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        )
    except (RuntimeError, OSError) as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
