"""Run offline SAFL regressions; --all also runs/skips the existing suite."""
import argparse
import os
import socket
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--junitxml")
    args = parser.parse_args()
    for key in list(os.environ):
        if key.startswith(("AWS_", "BEDROCK_", "CRDB_", "GOOGLE_", "GCP_")):
            os.environ.pop(key, None)
    os.environ["CRDB_URL"] = ""
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    def deny_network(*args, **kwargs):
        raise AssertionError("Network forbidden during offline SAFL validation")
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    socket.create_connection = deny_network
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    sys.path.insert(0, str(root))
    import pytest
    flags = ["tests" if args.all else "tests/unit/test_safl_safety.py",
             "-q", "-rs", "-p", "no:cacheprovider"]
    if args.junitxml:
        flags.append("--junitxml=" + args.junitxml)
    return pytest.main(flags)


if __name__ == "__main__":
    raise SystemExit(main())
