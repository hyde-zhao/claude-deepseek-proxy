#!/usr/bin/env python3
"""Local process manager for claude_deepseek_proxy_http.py."""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
PROXY_SCRIPT = PROJECT_DIR / "claude_deepseek_proxy_http.py"
STATE_DIR = Path.home() / ".claude-deepseek-proxy"
PID_FILE = STATE_DIR / "proxy.pid"
LOG_FILE = STATE_DIR / "proxy.log"
BIN_DIR = Path.home() / ".local" / "bin"
COMMAND_NAME = "claude-deepseek-proxy"


def _read_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _is_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _remove_stale_pid():
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _ensure_no_proxy(env):
    values = ["127.0.0.1", "localhost"]
    for key in ("NO_PROXY", "no_proxy"):
        existing = [v.strip() for v in env.get(key, "").split(",") if v.strip()]
        for value in values:
            if value not in existing:
                existing.append(value)
        env[key] = ",".join(existing)


def _command(port):
    uv = shutil.which("uv")
    if uv:
        cmd = [uv, "run", "python", str(PROXY_SCRIPT)]
    else:
        python = shutil.which("python3") or sys.executable
        cmd = [python, str(PROXY_SCRIPT)]

    if port:
        cmd.append(str(port))
    return cmd


def start(args):
    if not PROXY_SCRIPT.exists():
        print(f"proxy script not found: {PROXY_SCRIPT}", file=sys.stderr)
        return 1

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    pid = _read_pid()
    if _is_running(pid):
        print(f"{COMMAND_NAME} is already running: pid={pid}")
        return 0
    _remove_stale_pid()

    env = os.environ.copy()
    _ensure_no_proxy(env)

    log = LOG_FILE.open("ab")
    cmd = _command(args.port)
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    PID_FILE.write_text(f"{proc.pid}\n", encoding="utf-8")

    time.sleep(args.wait)
    if not _is_running(proc.pid):
        print(f"{COMMAND_NAME} failed to start. Log: {LOG_FILE}", file=sys.stderr)
        _remove_stale_pid()
        return 1

    print(f"{COMMAND_NAME} started: pid={proc.pid}")
    print(f"base url: http://127.0.0.1:{args.port}")
    print(f"log: {LOG_FILE}")
    return 0


def stop(args):
    pid = _read_pid()
    if not _is_running(pid):
        _remove_stale_pid()
        print(f"{COMMAND_NAME} is not running")
        return 0

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_stale_pid()
        print(f"{COMMAND_NAME} is not running")
        return 0

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if not _is_running(pid):
            _remove_stale_pid()
            print(f"{COMMAND_NAME} stopped")
            return 0
        time.sleep(0.2)

    if args.force:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        _remove_stale_pid()
        print(f"{COMMAND_NAME} killed")
        return 0

    print(f"{COMMAND_NAME} did not stop within {args.timeout}s; retry with --force", file=sys.stderr)
    return 1


def status(_args):
    pid = _read_pid()
    if _is_running(pid):
        print(f"{COMMAND_NAME} is running: pid={pid}")
        print(f"log: {LOG_FILE}")
        return 0

    _remove_stale_pid()
    print(f"{COMMAND_NAME} is not running")
    return 3


def restart(args):
    code = stop(args)
    if code not in (0,):
        return code
    return start(args)


def logs(args):
    if not LOG_FILE.exists():
        print(f"log file does not exist: {LOG_FILE}", file=sys.stderr)
        return 1

    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.lines:]:
        print(line)
    return 0


def install(_args):
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / COMMAND_NAME
    python = shutil.which("python3") or sys.executable
    wrapper = f"""#!/usr/bin/env bash
exec {python!r} {str(Path(__file__).resolve())!r} "$@"
"""
    target.write_text(wrapper, encoding="utf-8")
    target.chmod(0o755)
    print(f"installed: {target}")
    if str(BIN_DIR) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"add this to PATH if needed: export PATH=\"$HOME/.local/bin:$PATH\"")
    print(f"try: {COMMAND_NAME} start")
    return 0


def uninstall(_args):
    target = BIN_DIR / COMMAND_NAME
    try:
        target.unlink()
        print(f"removed: {target}")
    except FileNotFoundError:
        print(f"not installed: {target}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog=COMMAND_NAME,
        description="Manage claude-deepseek-proxy as a local background service.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", help="start the proxy in the background")
    p.add_argument("--port", type=int, default=9191)
    p.add_argument("--wait", type=float, default=0.6, help="seconds to wait before checking startup")
    p.set_defaults(func=start)

    p = sub.add_parser("stop", help="stop the background proxy")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=stop)

    p = sub.add_parser("restart", help="restart the background proxy")
    p.add_argument("--port", type=int, default=9191)
    p.add_argument("--wait", type=float, default=0.6)
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=restart)

    p = sub.add_parser("status", help="show whether the proxy is running")
    p.set_defaults(func=status)

    p = sub.add_parser("logs", help="print recent proxy logs")
    p.add_argument("-n", "--lines", type=int, default=80)
    p.set_defaults(func=logs)

    p = sub.add_parser("install", help=f"install {COMMAND_NAME} into ~/.local/bin")
    p.set_defaults(func=install)

    p = sub.add_parser("uninstall", help=f"remove {COMMAND_NAME} from ~/.local/bin")
    p.set_defaults(func=uninstall)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
