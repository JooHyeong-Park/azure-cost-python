#!/usr/bin/env python3
"""소스 체크아웃에서 Azure Cost Management 패키지를 실행한다."""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import import_module
from pathlib import Path
from typing import cast

# src 디렉터리의 모듈을 import 할 수 있도록 sys.path 에 추가
SRC_DIR = str(Path(__file__).resolve().parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

# src 경로 등록 후 애플리케이션 진입점 import
main = cast(Callable[[], int], import_module("azure_cost_management.main").main)


if __name__ == "__main__":
    raise SystemExit(main())
