"""Replay identical offline SAFL checks on the July baseline and current source.

Uses only local Git history, temporary files and the current Python environment.
No fetch, install, live provider, database, public service or source rewrite.
"""
import argparse
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASELINE = "96ba8f8b5ac7b1388ef8d59d5377e844f9f9ac56"
HARNESS = ("tests/safl_fixtures.py", "tests/unit/test_safl_safety.py",
           "tools/run_safl_checks.py", "tools/safl_demo.py")
CHANGED_SOURCE = ("quorum/memory/base.py", "quorum/memory/quorum.py", "quorum/policy/engine.py")


def run_checks(root, output, label):
    xml = output / f"{label}-regressions.xml"
    with (output / f"{label}-regressions.txt").open("w", encoding="utf-8") as log:
        run = subprocess.run([sys.executable, str(root / "tools/run_safl_checks.py"),
                              "--junitxml", str(xml)], cwd=root, stdout=log,
                             stderr=subprocess.STDOUT, timeout=120)
    cases = list(ET.parse(xml).getroot().iter("testcase"))
    failed = sum(c.find("failure") is not None for c in cases)
    errors = sum(c.find("error") is not None for c in cases)
    skipped = sum(c.find("skipped") is not None for c in cases)
    demo = output / f"{label}-demo.json"
    subprocess.run([sys.executable, str(root / "tools/safl_demo.py"), "--output", str(demo)],
                   cwd=root, check=True, stdout=subprocess.DEVNULL, timeout=60)
    return {"exit_code":run.returncode, "tests":len(cases), "failed":failed,
            "errors":errors, "skipped":skipped,
            "passed":len(cases)-failed-errors-skipped}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/safl-evidence")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (root / args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(root), "-c", "safe.directory=" + root.as_posix()]
    archive = subprocess.check_output(git + ["archive", "--format=tar", BASELINE])
    with tempfile.TemporaryDirectory(prefix="safl-baseline-", dir=output) as dirname:
        baseline = Path(dirname).resolve()
        if not baseline.is_relative_to(output):
            raise RuntimeError("Temporary checkout must remain in the evidence directory")
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(baseline, filter="data")
        for relative in HARNESS:
            destination = baseline / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / relative, destination)
        before = run_checks(baseline, output, "baseline")
    after = run_checks(root, output, "repaired")
    files = {name:hashlib.sha256((root / name).read_bytes()).hexdigest()
             for name in (*CHANGED_SOURCE, *HARNESS, "tools/safl_compare.py")}
    result = {"executed_at_utc":datetime.now(timezone.utc).isoformat(),
              "baseline_commit":BASELINE, "current_head":subprocess.check_output(
                  git + ["rev-parse", "HEAD"], text=True).strip(),
              "current_source":"working tree; hashes identify uncommitted changes",
              "python":sys.version, "baseline":before, "repaired":after,
              "source_and_harness_sha256":files,
              "database_integration_verified":False, "live_llm_calls":0}
    (output / "comparison.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if before["exit_code"] == 1 and before["errors"] == 0 and after["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
