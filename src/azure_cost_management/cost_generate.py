"""Azure 비용 행을 임시 CSV로 생성한다."""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

# Cost Management 응답에서 grouping 차원으로 사용할 컬럼
DIMENSION_COLUMNS = (
    "ResourceGroup",
    "ResourceType",
    "ServiceName",
    "MeterCategory",
    "MeterSubCategory",
    "Meter",
    "ResourceId",
)

# Cost Management 응답에서 유지할 컬럼과 CSV 출력 순서
AZURE_COLUMNS = ("CostUSD", "UsageDate", *DIMENSION_COLUMNS)
CostRow = dict[str, Any]
CustomFieldResolver = Callable[[CostRow, Mapping[str, Any]], Any]


def _resource_name(row: CostRow, _target: Mapping[str, Any]) -> str:
    """리소스 ID 의 마지막 두 경로를 반환한다."""

    resource_id = row.get("ResourceId")
    if not isinstance(resource_id, str):
        return ""
    parts = [part for part in resource_id.split("/") if part]
    return "/".join(parts[-2:])


@dataclass(frozen=True)
class CustomField:
    """사용자 정의 CSV 컬럼과 값 계산 방법."""

    name: str
    resolver: CustomFieldResolver


# 사용자 정의 필드는 이 목록에 이름과 resolver 를 함께 추가한다.
CUSTOM_FIELDS = (
    CustomField("ResourceName", _resource_name),
    CustomField("SubscriptionId", lambda _row, target: target["subscription_id"]),
    CustomField("SubscriptionName", lambda _row, target: target["subscription_name"]),
)
CUSTOM_COLUMNS = tuple(field.name for field in CUSTOM_FIELDS)
OUTPUT_COLUMNS = AZURE_COLUMNS + CUSTOM_COLUMNS


def add_custom_fields(row: CostRow, target: object) -> None:
    """API 응답 행에 사용자 정의 필드를 추가한다.

    resolver 에는 target 의 속성을 담은 context 를 전달한다.
    """

    target_values = vars(target)
    for field in CUSTOM_FIELDS:
        row[field.name] = field.resolver(row, target_values)


@dataclass(frozen=True)
class GenerateResult:
    """생성된 임시 CSV와 집계 결과."""

    temporary_path: Path
    file_name: str
    rows_written: int
    total_cost_usd: str


class CostGenerator(Protocol):
    """비용 행 생성 인터페이스."""

    def generate(
        self,
        start_date: str,
        end_date: str,
        rows: Iterable[CostRow],
    ) -> GenerateResult: ...


class CsvCostGenerator:
    """모든 조회 대상의 비용 행을 임시 CSV로 생성한다."""

    temp_dir: Path | None
    file_prefix: str

    def __init__(
        self,
        temp_dir: Path | None = None,
        file_prefix: str = "_raw-cost",
    ) -> None:
        self.temp_dir = temp_dir
        self.file_prefix = file_prefix

    def generate(
        self,
        start_date: str,
        end_date: str,
        rows: Iterable[CostRow],
    ) -> GenerateResult:
        """비용 행을 임시 CSV로 생성하고 집계 결과를 반환한다."""

        if start_date == end_date:
            file_name = f"{self.file_prefix}-{start_date}.csv"
        else:
            file_name = (
                f"{self.file_prefix}-{start_date}-{end_date}.csv"
            )
        write_dir = self.temp_dir or Path(tempfile.gettempdir())
        write_dir.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        rows_written = 0
        total_cost = Decimal(0)

        try:
            # Excel 호환 UTF-8 BOM 을 포함한 임시 CSV 작성
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8-sig",
                newline="",
                prefix=f".{file_name}.",
                suffix=".tmp",
                dir=write_dir,
                delete=False,
            ) as output_file:
                temporary_path = Path(output_file.name)
                writer = csv.writer(output_file, lineterminator="\n")
                writer.writerow(OUTPUT_COLUMNS)
                for row in rows:
                    writer.writerow(row.get(column) for column in OUTPUT_COLUMNS)
                    rows_written += 1
                    try:
                        total_cost += Decimal(str(row["CostUSD"]))
                    except (InvalidOperation, KeyError) as exc:
                        raise RuntimeError(
                            f"invalid CostUSD value: {row.get('CostUSD')!r}"
                        ) from exc
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise

        return GenerateResult(temporary_path, file_name, rows_written, str(total_cost))


def create_generator(config: dict[str, object], base_dir: Path) -> CostGenerator:
    """생성 설정을 검증하고 생성기를 만든다."""

    store_type = config.get("type")
    if store_type != "csv":
        raise RuntimeError(f"unsupported generate type: {store_type}")

    temp_dir = config.get("temp_dir")
    temp_path: Path | None = None
    if temp_dir is not None:
        if not isinstance(temp_dir, str) or not temp_dir:
            raise RuntimeError("generate.temp_dir must be a non-empty string")
        temp = Path(temp_dir)
        temp_path = temp if temp.is_absolute() else base_dir / temp

    file_prefix = config.get("file_prefix", "_raw-cost")
    if not isinstance(file_prefix, str) or not file_prefix.strip():
        raise RuntimeError("generate.file_prefix must be a non-empty string")
    return CsvCostGenerator(temp_path, file_prefix.strip())
