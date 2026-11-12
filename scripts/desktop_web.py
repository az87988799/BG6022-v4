"""Open the existing local webpage, starting its windowless server when needed.

Windows login launches --serve; the desktop shortcut launches --open. Neither
action resumes a Run, invokes a model, or starts a scientific process.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from filelock import FileLock

from orca_agent.config import load_config
from orca_agent.web import serve, service_identity

PROJECT = Path(__file__).resolve().parents[1]
READY_SECONDS = 20
HEALTH_BYTES = 4096


class LaunchError(RuntimeError):
    """A bounded local startup failed; existing processes and Runs are untouched."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _url(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LaunchError("网页端口必须在 1024 至 65535 之间")
    return f"http://127.0.0.1:{port}"


def _logs(identity):
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    target = base / "orca-agent" / "web" / identity["workspace"][:16]
    target.mkdir(parents=True, exist_ok=True)
    return target


def _probe(port, expected, *, timeout=3):
    # Loopback only, even when system proxy environment variables are configured.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(_url(port) + "/health", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(HEALTH_BYTES + 1)
            if response.status != 200 or len(raw) > HEALTH_BYTES:
                raise LaunchError("该端口未返回本项目的网页服务标识")
            try:
                actual = json.loads(raw)
            except (ValueError, UnicodeError):
                raise LaunchError("该端口未返回本项目的网页服务标识") from None
            if actual != expected:
                raise LaunchError("端口已被其他工作区或程序使用，未启动重复服务")
            return True
    except urllib.error.HTTPError:
        raise LaunchError("端口已有其他或旧版服务，未替换、关闭或跳转该服务") from None
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
            return False
        raise LaunchError("本地网页服务未能在期限内响应，请查看启动日志") from None
    except TimeoutError:
        raise LaunchError("本地网页服务未能在期限内响应，请查看启动日志") from None


def _spawn(config_path, port, log_dir):
    pythonw = PROJECT / ".venv/Scripts/pythonw.exe"
    if not pythonw.is_file():
        raise LaunchError("未找到项目的 Python 无窗口运行环境")
    with (log_dir / "launcher.log").open("a", encoding="utf-8") as output:
        return subprocess.Popen(
            [str(pythonw), str(Path(__file__).resolve()), "--serve", "--config", str(config_path),
             "--port", str(port)], cwd=PROJECT, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), shell=False,
        )


def ensure_server(config_path, port, *, timeout=READY_SECONDS):
    config = load_config(config_path)
    expected = service_identity(config.data_root)
    log_dir = _logs(expected)
    # Concurrent desktop clicks share startup only; the server keeps its
    # original Store/service lock and never owns this short launcher lock.
    with FileLock(str(log_dir / "start.lock"), timeout=timeout):
        if _probe(port, expected):
            return {"url": _url(port), "started": False, "log_directory": str(log_dir)}
        process = _spawn(config_path, port, log_dir)
        deadline = time.monotonic() + timeout
        child_exited = False
        while time.monotonic() < deadline:
            if _probe(port, expected, timeout=min(3, max(.05, deadline - time.monotonic()))):
                return {"url": _url(port), "started": True, "log_directory": str(log_dir)}
            # Login startup may win the original service lock while our child
            # exits. Keep waiting for that same healthy service, without spawning
            # another child or extending the original readiness deadline.
            child_exited = child_exited or process.poll() is not None
            time.sleep(.15)
        if child_exited:
            raise LaunchError("后台网页服务启动失败，请查看启动日志")
    raise LaunchError("后台网页服务尚未就绪，请查看启动日志；没有重复启动或结束进程")


def serve_background(config_path, port):
    config = load_config(config_path)
    expected = service_identity(config.data_root)
    if _probe(port, expected):
        return
    log_dir = _logs(expected)
    output_path = log_dir / "web.log"
    # No console exists under pythonw. Uvicorn's formatter still requires
    # usable stdout/stderr streams, so bind them before it configures logging.
    if output_path.exists() and output_path.stat().st_size > 2 * 1024 * 1024:
        output_path.replace(log_dir / "web.previous.log")
    with output_path.open("a", encoding="utf-8", buffering=1) as output:
        with redirect_stdout(output), redirect_stderr(output):
            serve(config, port=port)


def _notify_error(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "ORCA 分子工作台", 0x10)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open or start the existing local ORCA webpage")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="Serve without opening a browser")
    mode.add_argument("--ensure", action="store_true", help="Ensure the local server is ready and exit")
    mode.add_argument("--open", action="store_true", help="Ensure readiness, then open the webpage")
    parser.add_argument("--config", type=Path, default=PROJECT / "config.local.toml")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    os.chdir(PROJECT)
    config_path = args.config.resolve()
    try:
        _url(args.port)
        if not config_path.is_file():
            raise LaunchError("未找到本机网页配置文件")
        if args.serve:
            serve_background(config_path, args.port)
        else:
            result = ensure_server(config_path, args.port)
            if not args.ensure and not webbrowser.open(result["url"], new=2):
                raise LaunchError("服务已就绪，但默认浏览器未能打开，请直接访问 " + result["url"])
            if sys.stdout is not None:
                print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, LaunchError) else "网页启动失败：" + type(error).__name__
        if sys.stderr is not None:
            print(message, file=sys.stderr)
        try:
            identity = service_identity(load_config(config_path).data_root)
            with (_logs(identity) / "launcher.log").open("a", encoding="utf-8") as output:
                output.write(message + "\n")
        except (ValueError, OSError):
            pass
        if not args.serve and not args.ensure:
            _notify_error(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
