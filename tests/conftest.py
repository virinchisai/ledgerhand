"""Shared fixtures.

The mock target app is started once per session as a subprocess, because the
tests that matter most are the ones that drive a real browser against real
markup -- a fake surface would not exercise frame traversal, label recovery, or
any of the runtime conditions this system exists to handle.
"""
from __future__ import annotations

import os
import pathlib
import socket
import subprocess
import sys
import time

import httpx
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8848"


def _up() -> bool:
    try:
        return httpx.get(f"{BASE}/admin/health", timeout=1.0).status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session")
def app_server() -> str:
    if _up():
        yield BASE
        return
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "apps" / "legacy_bank" / "server.py")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        if _up():
            break
        time.sleep(0.25)
    else:
        proc.terminate()
        pytest.skip("mock app did not start")
    yield BASE
    proc.terminate()


@pytest.fixture(autouse=True)
def _reset_app(app_server: str):
    """Every test starts from the same app state, including chaos flags.

    Runs after module-scoped fixtures, so a module that records an artifact
    once still gets a clean app for each test that replays it.
    """
    httpx.post(f"{app_server}/admin/reset", timeout=5.0)
    yield


@pytest.fixture
def chaos(app_server: str):
    def _set(**flags):
        httpx.post(f"{app_server}/admin/chaos", json=flags, timeout=5.0)
    return _set


@pytest.fixture(scope="session", autouse=True)
def _demo_credentials():
    os.environ.setdefault("LEDGERHAND_MCB_OPERATOR", "svc.automation")
    os.environ.setdefault("LEDGERHAND_MCB_PASSWORD", "Sandbox!Demo1")
