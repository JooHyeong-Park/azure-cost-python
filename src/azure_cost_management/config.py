"""TOML 설정을 검증해 비용 조회 실행 설정으로 변환한다."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

from .cost_generate import (
    CostGenerator,
    create_generator,
)

API_VERSION = "2025-03-01"
HTTP_TIMEOUT_SECONDS = 60.0
HTTP_MAX_ATTEMPTS = 6
HTTP_RETRY_WAIT_SECONDS = 30.0

# 날짜 생략 시 조회할 과거 일수
DEFAULT_LOOKBACK_DAYS = 7


class ConfigError(RuntimeError):
    """설정 파일의 구조 또는 값이 실행 조건을 충족하지 않을 때 발생한다."""

    pass


@dataclass(frozen=True)
class Target:
    """하나의 구독에 적용할 비용 조회 조건."""

    subscription_id: str
    subscription_name: str
    resource_groups: tuple[str, ...]
    resource_types: tuple[str, ...]
    resource_id_prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Settings:
    """검증 완료된 비용 조회 실행 설정."""

    start_date: str
    end_date: str
    targets: tuple[Target, ...]
    generate: CostGenerator
    save: dict[str, object]
    timeout: float
    max_attempts: int
    retry_wait: float
    api_version: str


def _text(value: object, name: str) -> str:
    """필수 문자열의 양쪽 공백을 제거하고 빈 값을 거부한다."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _strings(value: object, name: str) -> tuple[str, ...]:
    """선택 문자열 배열을 정리해 tuple 로 변환한다."""

    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _parse_date(value: object, name: str) -> str:
    """날짜를 검증해 YYYYMMDD 형식으로 정규화한다."""

    text = _text(value, name)
    try:
        if re.fullmatch(r"\d{8}", text):
            parsed = datetime.strptime(text, "%Y%m%d").date()
        else:
            parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ConfigError(f"{name} must be YYYYMMDD or YYYY-MM-DD") from exc
    return parsed.strftime("%Y%m%d")


def load_settings(
    config_path: Path,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Settings:
    """TOML 파일을 읽어 전체 실행 설정을 생성한다."""

    try:
        with config_path.open("rb") as config_file:
            config = cast(dict[str, object], tomllib.load(config_file))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {config_path}: {exc}") from exc

    # 기간 미지정 시 7일 전 하루, CLI 인자가 설정보다 우선
    start_value = config.get("start_date")
    end_value = config.get("end_date")
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ConfigError("start_date and end_date must be provided together")
        start_value = start_date
        end_value = end_date

    if start_value is None and end_value is None:
        default_date = date.today() - timedelta(days=DEFAULT_LOOKBACK_DAYS)
        start_value = end_value = default_date.strftime("%Y%m%d")
    elif start_value is None or end_value is None:
        raise ConfigError("start_date and end_date must be provided together")

    start_date = _parse_date(start_value, "start_date")
    end_date = _parse_date(end_value, "end_date")
    if start_date > end_date:
        raise ConfigError("start_date must not be later than end_date")

    # 조회 대상
    raw_targets = config.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ConfigError("targets must be a non-empty array")
    targets: list[Target] = []
    for index, raw_target in enumerate(raw_targets):
        if not isinstance(raw_target, dict):
            raise ConfigError(f"targets[{index}] must be an object")
        targets.append(
            Target(
                subscription_id=_text(
                    raw_target.get("subscription_id"),
                    f"targets[{index}].subscription_id",
                ),
                subscription_name=_text(
                    raw_target.get("subscription_name"),
                    f"targets[{index}].subscription_name",
                ),
                resource_groups=_strings(
                    raw_target.get("resource_groups"),
                    f"targets[{index}].resource_groups",
                ),
                resource_types=_strings(
                    raw_target.get("resource_types"),
                    f"targets[{index}].resource_types",
                ),
                resource_id_prefixes=_strings(
                    raw_target.get("resource_id_prefixes"),
                    f"targets[{index}].resource_id_prefixes",
                ),
            )
        )

    # 생성/저장소 설정
    base_dir = config_path.resolve().parent
    generate_config = config.get("generate")
    if not isinstance(generate_config, dict):
        raise ConfigError("generate must be an object")
    raw_save_config = config.get("save")
    if not isinstance(raw_save_config, dict):
        raise ConfigError("save must be an object")
    save_config = cast(dict[str, object], raw_save_config)
    save_config = {**save_config, "_base_dir": base_dir}
    return Settings(
        start_date=start_date,
        end_date=end_date,
        targets=tuple(targets),
        generate=create_generator(generate_config, base_dir),
        save=save_config,
        timeout=HTTP_TIMEOUT_SECONDS,
        max_attempts=HTTP_MAX_ATTEMPTS,
        retry_wait=HTTP_RETRY_WAIT_SECONDS,
        api_version=_text(config.get("api_version", API_VERSION), "api_version"),
    )
