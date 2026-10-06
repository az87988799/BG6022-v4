"""Offline by default; explicit gates for scientific execution."""

import os
import socket
import subprocess
import traceback
from pathlib import Path

import pytest


def pytest_addoption(parser):
    parser.addoption("--live-orca", action="store_true", help="Enable real ORCA evidence tests")
    parser.addoption("--live-model", action="store_true", help="Enable bounded DeepSeek HTTPS tests")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("model") or item.get_closest_marker("e2e"):
            if not config.getoption("--live-model"):
                item.add_marker(pytest.mark.skip(reason="real model not enabled; unverified"))
        if item.get_closest_marker("e2e") and not config.getoption("--live-orca"):
            item.add_marker(pytest.mark.skip(reason="joint real ORCA not enabled; unverified"))
        if item.get_closest_marker("live") and not config.getoption("--live-orca"):
            item.add_marker(pytest.mark.skip(reason="real ORCA not enabled; unverified"))


@pytest.fixture(autouse=True)
def execution_boundary(request, monkeypatch):
    def denied(*args, **kwargs):
        raise RuntimeError("offline test boundary: network/process execution prohibited")

    model = request.node.get_closest_marker("model") or request.node.get_closest_marker("e2e")
    science = request.node.get_closest_marker("live") or request.node.get_closest_marker("e2e")
    if model and request.config.getoption("--live-model"):
        if not os.environ.get("DEEPSEEK_API_KEY"):
            pytest.skip("model key missing; unverified")
        getaddrinfo, connect = socket.getaddrinfo, socket.socket.connect
        addresses = set()

        def scoped_dns(host, port, *args, **kwargs):
            if host not in ("api.deepseek.com", b"api.deepseek.com") or port not in (443, "443"):
                return denied()
            records = getaddrinfo(host, port, *args, **kwargs)
            addresses.update(record[4][0] for record in records)
            return records

        def scoped_connect(sock, address):
            # Windows stdlib builds asyncio's internal wakeup socketpair over
            # loopback. Only its own implementation may create that local pair.
            internal_pair = (address[0] in {"127.0.0.1", "::1"}
                             and any(frame.name in {"socketpair", "_socketpair"}
                                     and Path(frame.filename).resolve() == Path(socket.__file__).resolve()
                                     for frame in traceback.extract_stack()))
            if not internal_pair and not (address[0] in addresses and address[1] == 443):
                return denied()
            return connect(sock, address)

        monkeypatch.setattr(socket, "getaddrinfo", scoped_dns)
        monkeypatch.setattr(socket.socket, "connect", scoped_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", denied)
    else:
        monkeypatch.setattr(socket, "create_connection", denied)
        monkeypatch.setattr(socket.socket, "connect", denied)
        monkeypatch.setattr(socket.socket, "connect_ex", denied)
        monkeypatch.setattr(socket, "getaddrinfo", denied)
    if science:
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
