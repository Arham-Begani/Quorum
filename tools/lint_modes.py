"""CI lint for invariant I8: nothing outside factory.py may branch on mode.

The three modes must share one workload driver, one seed and one agent
implementation. The moment a `if mode == "naive"` appears in the driver, the
agents, or the detectors, the three columns stop being the same experiment and
the comparison stops being evidence of anything.

    python tools/lint_modes.py

Exits non-zero on a violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "quorum"

ALLOWED = {
    PACKAGE / "memory" / "factory.py",      # the one legal place [I8]
}

# `mode == "naive"`, `mode != 'quorum'`, `mode in ("naive", ...)`, and the
# dict-dispatch equivalents keyed on a mode literal.
PATTERNS = [
    re.compile(r"\bmode\s*[!=]=\s*['\"](naive|txn_only|quorum)['\"]"),
    re.compile(r"['\"](naive|txn_only|quorum)['\"]\s*[!=]=\s*\bmode\b"),
    re.compile(r"\bmode\s+(not\s+)?in\s*[\(\[\{]"),
]


def scan() -> list[tuple[Path, int, str]]:
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path in ALLOWED:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pat in PATTERNS:
                if pat.search(line):
                    violations.append((path.relative_to(ROOT), lineno, stripped))
                    break
    return violations


def main() -> int:
    violations = scan()
    if not violations:
        print(f"I8 OK — no mode branching outside "
              f"{', '.join(str(p.relative_to(ROOT)) for p in ALLOWED)}")
        return 0
    print("I8 VIOLATION — mode branching found outside factory.py.")
    print("The three modes must differ ONLY in the injected MemoryClient.\n")
    for path, lineno, line in violations:
        print(f"  {path}:{lineno}: {line}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
