"""Verify that the private environment matches pinned requirements exactly."""
from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REQ = ROOT / "requirements.txt"
PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s;#]+)")


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main() -> int:
    req = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_REQ
    if not req.exists():
        print(f"requirements file not found: {req}")
        return 2

    expected: dict[str, tuple[str, str]] = {}
    for line in req.read_text(encoding="utf-8").splitlines():
        match = PIN_RE.match(line)
        if match:
            package, version = match.groups()
            expected[normalized(package)] = (package, version)

    problems: list[str] = []
    for _, (package, wanted) in sorted(expected.items()):
        try:
            installed = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            problems.append(f"missing {package}=={wanted}")
            continue
        if installed != wanted:
            problems.append(f"{package}: installed {installed}, required {wanted}")

    if problems:
        for problem in problems:
            print(problem)
        return 1

    print(f"Verified {len(expected)} pinned dependencies from {req.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
