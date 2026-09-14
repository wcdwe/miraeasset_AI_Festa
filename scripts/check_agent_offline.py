"""Offline unit checks. Network is blocked before any project import.

This runner NEVER loads/runs the 100-question paid evaluation suite.
"""
import sys
from pathlib import Path
import unittest


def deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("OFFLINE_CHECK: network access forbidden")


def main():
    sys.addaudithook(deny_network)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    pattern = sys.argv[1] if len(sys.argv) > 1 else "test_pipeline_contracts.py"
    suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__": raise SystemExit(main())
