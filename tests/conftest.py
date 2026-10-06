"""Offline by default; explicit gates for scientific execution."""

import os
import socket
import subprocess
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--live-orca", action="store_true", help="Enable real ORCA evidence tests")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("live") and not config.getoption("--live-orca"):
            item.add_marker(pytest.mark.skip(reason="real ORCA not enabled; unverified"))


@pytest.fixture(autouse=True)
def execution_boundary(request, monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError("offline test boundary: network/process execution prohibited")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    if request.node.get_closest_marker("live"):
        if not request.config.getoption("--live-orca"):
            pytest.skip("real ORCA not enabled; unverified")
        if not Path(os.environ.get("ORCA_AGENT_ORCA", "E:/orca/orca.exe")).is_file():
            pytest.skip("ORCA missing; scientific behavior unverified")
    elif not request.node.get_closest_marker("backend"):
        monkeypatch.setattr(subprocess, "Popen", denied)
        monkeypatch.setattr(os, "system", denied)
        monkeypatch.setattr(os, "popen", denied)
        from orca_agent.backends import local
        monkeypatch.setattr(local, "_native", denied)
