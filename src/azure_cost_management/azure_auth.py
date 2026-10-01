"""Azure SDK 없이 ARM Access Token 을 발급하고 캐시한다."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

AUTHORITY_HOST = "https://login.microsoftonline.com"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
MANAGEMENT_RESOURCE = "https://management.azure.com/"


class TokenProvider:
    """만료 전 갱신을 지원하는 Access Token 공급자."""

    can_refresh: bool = True
    token: str
    expires_at: float

    def __init__(self) -> None:
        self.token = ""
        self.expires_at = 0.0

    def get_token(self, force_refresh: bool = False) -> str:
        """사용 가능한 캐시 토큰을 반환하거나 새 토큰을 발급한다."""

        # 잔여 유효 시간 5분 이상 시 캐시 토큰 재사용
        if not force_refresh and self.token and self.expires_at - time.time() > 300:
            return self.token
        self.token, self.expires_at = self._acquire()
        return self.token

    def _acquire(self) -> tuple[str, float]:
        raise NotImplementedError


class StaticTokenProvider(TokenProvider):
    """환경 변수 또는 파일로 전달된 Access Token 공급자."""

    can_refresh: bool = False
    token: str
    expires_at: float

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token
        self.expires_at = float("inf")

    def _acquire(self) -> tuple[str, float]:
        return self.token, self.expires_at


class WorkloadIdentityProvider(TokenProvider):
    """Kubernetes ServiceAccount 토큰을 ARM Access Token 으로 교환한다."""

    tenant_id: str
    client_id: str
    token_file: Path
    authority_host: str
    scope: str
    timeout: float

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        token_file: Path,
        authority_host: str,
        scope: str,
        timeout: float,
    ) -> None:
        super().__init__()
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.token_file = token_file
        self.authority_host = authority_host.rstrip("/")
        self.scope = scope
        self.timeout = timeout

    def _acquire(self) -> tuple[str, float]:
        """Federated Token 파일을 읽어 ARM Access Token 을 발급한다."""

        try:
            assertion = self.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read {self.token_file}: {exc}") from exc
        if not assertion:
            raise RuntimeError("federated token file is empty")

        return _request_token(
            f"{self.authority_host}/{self.tenant_id}/oauth2/v2.0/token",
            {
                "client_id": self.client_id,
                "scope": self.scope,
                "grant_type": "client_credentials",
                "client_assertion_type": (
                    "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
                ),
                "client_assertion": assertion,
            },
            self.timeout,
        )


class ManagedIdentityProvider(TokenProvider):
    """Azure Managed Identity endpoint 에서 ARM Access Token 을 발급한다."""

    client_id: str | None
    resource: str
    timeout: float

    def __init__(self, client_id: str | None, resource: str, timeout: float) -> None:
        super().__init__()
        self.client_id = client_id
        self.resource = resource
        self.timeout = min(timeout, 10.0)

    def _acquire(self) -> tuple[str, float]:
        """실행 환경의 Managed Identity endpoint 를 선택해 토큰을 요청한다."""

        identity_endpoint = os.getenv("IDENTITY_ENDPOINT")
        identity_header = os.getenv("IDENTITY_HEADER")
        msi_endpoint = os.getenv("MSI_ENDPOINT")
        msi_secret = os.getenv("MSI_SECRET")
        params = {"resource": self.resource}
        headers = {"Metadata": "true", "Accept": "application/json"}

        # App Service 및 Functions 의 IDENTITY endpoint
        if identity_endpoint and identity_header:
            url = identity_endpoint
            params["api-version"] = "2019-08-01"
            headers["X-IDENTITY-HEADER"] = identity_header
        # 기존 Azure 서비스의 MSI endpoint
        elif msi_endpoint and msi_secret:
            url = msi_endpoint
            params["api-version"] = "2017-09-01"
            headers["Secret"] = msi_secret
        else:
            # VM 및 VMSS 의 IMDS endpoint
            url = "http://169.254.169.254/metadata/identity/oauth2/token"
            params["api-version"] = "2018-02-01"

        if self.client_id:
            params["client_id"] = self.client_id
        separator = "&" if "?" in url else "?"
        request = Request(
            f"{url}{separator}{urlencode(params)}",
            headers=headers,
            method="GET",
        )
        return _parse_token(_request_json(request, self.timeout))


def _request_json(request: Request, timeout: float) -> dict[str, object]:
    """토큰 endpoint 의 JSON 객체 응답을 반환한다."""

    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"token request failed: HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"token request failed: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("token response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("token response must be a JSON object")
    return payload


def _parse_token(payload: dict[str, object]) -> tuple[str, float]:
    """인증 응답을 Access Token 과 Unix 만료 시각으로 변환한다."""

    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("token response did not contain access_token")
    try:
        # Managed Identity 는 expires_on, OAuth 는 expires_in 사용
        if payload.get("expires_on") is not None:
            expires_at = float(str(payload["expires_on"]))
        else:
            expires_at = time.time() + float(str(payload.get("expires_in", 3600)))
    except ValueError:
        # 만료값 변환 실패 시 기본 유효시간 1시간 적용
        expires_at = time.time() + 3600
    return token, expires_at


def _request_token(
    url: str, values: dict[str, str], timeout: float
) -> tuple[str, float]:
    """OAuth form 요청으로 Access Token 을 발급한다."""

    request = Request(
        url,
        data=urlencode(values).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "azure-cost-python/1.0",
        },
        method="POST",
    )
    return _parse_token(_request_json(request, timeout))


def _required_env(name: str) -> str:
    """필수 인증 환경 변수를 반환한다."""

    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _load_static_token() -> str:
    """파일 또는 환경 변수에서 사전 발급 Access Token 을 읽는다."""

    # 토큰 문자열 환경 변수보다 토큰 파일 우선
    token_file = os.getenv("AZURE_ACCESS_TOKEN_FILE")
    if token_file:
        try:
            token = Path(token_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read {token_file}: {exc}") from exc
    else:
        token = os.getenv("AZURE_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("AZURE_ACCESS_TOKEN_FILE or AZURE_ACCESS_TOKEN is required")
    return token


def create_token_provider(timeout: float) -> tuple[str, TokenProvider]:
    """인증 모드와 환경 변수에 맞는 토큰 공급자를 생성한다."""

    mode = os.getenv("AZURE_AUTH_MODE", "auto")
    if mode == "auto":
        # 자동 선택 순서: Access Token, Workload Identity, Managed Identity
        if os.getenv("AZURE_ACCESS_TOKEN_FILE") or os.getenv("AZURE_ACCESS_TOKEN"):
            mode = "access-token"
        elif os.getenv("AZURE_FEDERATED_TOKEN_FILE"):
            mode = "workload-identity"
        elif os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"):
            mode = "managed-identity"
        else:
            raise RuntimeError("cannot detect authentication; set AZURE_AUTH_MODE")

    if mode == "access-token":
        return mode, StaticTokenProvider(_load_static_token())
    if mode == "workload-identity":
        return mode, WorkloadIdentityProvider(
            _required_env("AZURE_TENANT_ID"),
            _required_env("AZURE_CLIENT_ID"),
            Path(_required_env("AZURE_FEDERATED_TOKEN_FILE")),
            AUTHORITY_HOST,
            MANAGEMENT_SCOPE,
            timeout,
        )
    if mode == "managed-identity":
        return mode, ManagedIdentityProvider(
            os.getenv("AZURE_CLIENT_ID"), MANAGEMENT_RESOURCE, timeout
        )
    raise RuntimeError(f"unsupported AZURE_AUTH_MODE: {mode}")
