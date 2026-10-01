"""생성된 임시 CSV 를 최종 경로에 저장한다."""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path


def save(
    temporary_path: Path,
    file_name: str,
    config: Mapping[str, object],
) -> Path:
    """저장 방식을 선택해 임시 CSV 를 최종 경로에 저장한다."""

    return _move(temporary_path, file_name, config)


def _move(
    temporary_path: Path,
    file_name: str,
    config: Mapping[str, object],
) -> Path:
    """임시 CSV 를 최종 경로로 이동한다."""

    output_dir = config.get("output_dir")
    if not isinstance(output_dir, str) or not output_dir:
        raise RuntimeError("save.output_dir must be a non-empty string")
    base_dir = config.get("_base_dir")
    output_path = Path(output_dir)
    if not output_path.is_absolute():
        if not isinstance(base_dir, Path):
            raise RuntimeError("save base directory is missing")
        output_path = base_dir / output_path
    output_path /= file_name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(temporary_path, output_path)
    except OSError:
        shutil.move(str(temporary_path), str(output_path))
    return output_path
