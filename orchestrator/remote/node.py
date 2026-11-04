#!/usr/bin/env python3
"""orchestrator-node: the orchestrator's agent on a build machine. Standard library only.

Invoked over ssh as `python3 node.py <operation> --root ROOT [--checkout CHECKOUT] ...`. Every
task directory lives at `<root>/<run>/<key>/`, holding `src/` (the source) and `jobs/` (pid
files). Nothing outside the root is written, except the git worktree registrations that `prepare`
and `clean` make in the checkout.

Operations:
  info                                  JSON about the machine, the root, and the checkout
  prepare RUN KEY BASE_SHA [--fetch-url U --fetch-ref R]
                                        a fresh task directory; src/ is a detached worktree of the
                                        checkout at BASE_SHA (fetched from U R when missing), or empty
  sync RUN KEY                          stdin: one JSON header line, then a tar stream for src/
  run RUN KEY JOB [--env K=V]... -- ARGV
                                        ARGV in src/, in its own process group; killed when stdin
                                        reaches EOF, which is what a dropped ssh connection looks like
  cancel RUN KEY JOB                    kill a job's process group
  clean RUN [KEY] | clean --older-than-days N
                                        remove task directories and their worktree registrations
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time

VERSION = 1
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
KEEP_ENV = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "SHELL", "TMPDIR", "PATH")
KILL_GRACE_SECONDS = 10


class Refused(Exception):
    """A request the node will not carry out; the message goes to stderr and the exit code is 2."""


def _name(value: str, what: str) -> str:
    if not NAME.match(value):
        raise Refused(f"invalid {what}: {value!r}")
    return value


def _task_dir(root: str, run: str, key: str) -> str:
    return os.path.join(root, _name(run, "run id"), _name(key, "issue key"))


def _git(checkout: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", checkout, *args], capture_output=True, text=True, encoding="utf-8", check=check
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _running_jobs(root: str) -> int:
    count = 0
    if not os.path.isdir(root):
        return 0
    for run in os.listdir(root):
        run_dir = os.path.join(root, run)
        if not os.path.isdir(run_dir):
            continue
        for key in os.listdir(run_dir):
            jobs = os.path.join(run_dir, key, "jobs")
            if not os.path.isdir(jobs):
                continue
            for f in os.listdir(jobs):
                try:
                    with open(os.path.join(jobs, f), encoding="utf-8") as fh:
                        if _alive(int(fh.read().strip())):
                            count += 1
                except (OSError, ValueError):
                    continue
    return count


# --- operations ---------------------------------------------------------------------------------


def op_info(args: argparse.Namespace) -> int:
    root = args.root
    usage_path = root if os.path.isdir(root) else os.path.dirname(root.rstrip("/")) or "/"
    usage = shutil.disk_usage(usage_path)
    checkout: dict[str, object] | None = None
    if args.checkout:
        exists = os.path.isdir(os.path.join(args.checkout, ".git"))
        remote = (
            _git(args.checkout, "remote", "get-url", "origin", check=False).stdout.strip() if exists else ""
        )
        checkout = {"path": args.checkout, "exists": exists, "remote": remote}
    print(
        json.dumps(
            {
                "version": VERSION,
                "hostname": socket.gethostname(),
                "os": platform.system(),
                "arch": platform.machine(),
                "python": platform.python_version(),
                "root": root,
                "root_exists": os.path.isdir(root),
                "free_bytes": usage.free,
                "checkout": checkout,
                "running_jobs": _running_jobs(root),
                "cpus": os.cpu_count(),
            }
        )
    )
    return 0


def _remove_task_dir(task_dir: str, checkout: str | None) -> None:
    src = os.path.join(task_dir, "src")
    if checkout and os.path.exists(os.path.join(src, ".git")):
        _git(checkout, "worktree", "remove", "--force", src, check=False)
    shutil.rmtree(task_dir, ignore_errors=True)
    if checkout:
        _git(checkout, "worktree", "prune", check=False)


def op_prepare(args: argparse.Namespace) -> int:
    task_dir = _task_dir(args.root, args.run, args.key)
    if not re.match(r"^[0-9a-f]{7,64}$", args.base_sha):
        raise Refused(f"invalid base commit: {args.base_sha!r}")
    _remove_task_dir(task_dir, args.checkout)
    os.makedirs(os.path.join(task_dir, "jobs"))
    src = os.path.join(task_dir, "src")
    if args.checkout:
        commit = f"{args.base_sha}^{{commit}}"
        if _git(args.checkout, "cat-file", "-e", commit, check=False).returncode != 0:
            if not args.fetch_url:
                raise Refused(f"{args.base_sha} is not in {args.checkout} and no fetch URL was given")
            fetched = _git(args.checkout, "fetch", "--no-tags", args.fetch_url, args.fetch_ref, check=False)
            if fetched.returncode != 0:
                raise Refused(f"git fetch {args.fetch_url} {args.fetch_ref} failed:\n{fetched.stderr}")
            if _git(args.checkout, "cat-file", "-e", commit, check=False).returncode != 0:
                raise Refused(f"{args.base_sha} is still missing after fetching {args.fetch_ref}")
        added = _git(args.checkout, "worktree", "add", "--detach", src, args.base_sha, check=False)
        if added.returncode != 0:
            raise Refused(f"git worktree add failed:\n{added.stderr}")
    else:
        os.makedirs(src)
    print(json.dumps({"task_dir": task_dir, "src": src, "from_checkout": bool(args.checkout)}))
    return 0


def _inside(src: str, rel: str) -> str:
    """The absolute path for a member, refusing anything that could land outside src."""
    if not rel or rel.startswith("/") or "\\" in rel:
        raise Refused(f"refused path {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts) or parts[0] == ".git":
        raise Refused(f"refused path {rel!r}")
    target = os.path.join(src, *parts)
    # an existing symlinked directory on the way must not lead out of src
    parent = os.path.realpath(os.path.dirname(target))
    real_src = os.path.realpath(src)
    if parent != real_src and not parent.startswith(real_src + os.sep):
        raise Refused(f"refused path {rel!r}: it leads outside the source directory")
    return target


def _unlink(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.unlink(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def op_sync(args: argparse.Namespace) -> int:
    src = os.path.join(_task_dir(args.root, args.run, args.key), "src")
    if not os.path.isdir(src):
        raise Refused(f"no task directory for {args.run}/{args.key}; prepare it first")
    stdin = sys.stdin.buffer
    header = json.loads(stdin.readline().decode())
    deleted = 0
    for rel in header.get("delete", []):
        target = _inside(src, rel)
        if os.path.lexists(target):
            _unlink(target)
            deleted += 1
    files = 0
    size = 0
    with tarfile.open(fileobj=stdin, mode="r|gz", encoding="utf-8") as tar:
        for member in tar:
            target = _inside(src, member.name)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if os.path.lexists(target):
                _unlink(target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if member.issym():
                link = os.path.normpath(os.path.join(os.path.dirname(member.name), member.linkname))
                if member.linkname.startswith("/") or link == ".." or link.startswith("../"):
                    raise Refused(f"refused symlink {member.name!r} -> {member.linkname!r}")
                os.symlink(member.linkname, target)
            elif member.isfile():
                data = tar.extractfile(member)
                assert data is not None
                with open(target, "wb") as out:
                    shutil.copyfileobj(data, out)
                os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
                size += member.size
            else:
                raise Refused(f"refused member type for {member.name!r}")
            files += 1
    print(json.dumps({"files": files, "bytes": size, "deleted": deleted}))
    return 0


def _venv_bin(src: str) -> str | None:
    for name in sorted(os.listdir(src)):
        if name.startswith("python-env-") and os.path.exists(os.path.join(src, name, "bin", "python")):
            return os.path.join(src, name, "bin")
    return None


def _kill_group(pgid: int) -> None:
    """SIGTERM a process group, then SIGKILL what is left after the grace period.

    A group holding only zombies answers EPERM on some systems, which means it is gone too.
    """
    if sys.platform == "win32":
        raise Refused("build machines are POSIX")
    for sig, wait in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, 0)):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.2)


def op_run(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        raise Refused("build machines are POSIX")
    task_dir = _task_dir(args.root, args.run, args.key)
    src = os.path.join(task_dir, "src")
    job = _name(args.job, "job name")
    if not os.path.isdir(src):
        raise Refused(f"no task directory for {args.run}/{args.key}; prepare it first")
    if not args.argv:
        raise Refused("nothing to run")
    env = {k: v for k, v in os.environ.items() if k in KEEP_ENV or k.startswith("LC_")}
    for pair in args.env:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            raise Refused(f"invalid --env {pair!r}")
        env[k] = v
    venv = _venv_bin(src)
    if venv:
        env["PATH"] = venv + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    try:
        proc = subprocess.Popen(args.argv, cwd=src, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
    except FileNotFoundError:
        print(f"orchestrator-node: command not found: {args.argv[0]}", file=sys.stderr)
        return 127
    pid_file = os.path.join(task_dir, "jobs", job + ".pid")
    with open(pid_file, "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))

    def watch_stdin() -> None:
        # the orchestrator holds stdin open for the job's whole life; EOF means it went away
        while os.read(0, 65536):  # the raw fd: a buffered reader's lock would hang interpreter exit
            pass
        if proc.poll() is None:
            _kill_group(proc.pid)

    def on_signal(signum: int, _frame: object) -> None:
        # the orchestrator or sshd is stopping this script; the job must not outlive it
        _kill_group(proc.pid)
        try:
            os.unlink(pid_file)
        except OSError:
            pass
        os._exit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        code = proc.wait()
    finally:
        try:
            os.unlink(pid_file)
        except OSError:
            pass
    return code if code >= 0 else 128 - code


def op_cancel(args: argparse.Namespace) -> int:
    pid_file = os.path.join(
        _task_dir(args.root, args.run, args.key), "jobs", _name(args.job, "job name") + ".pid"
    )
    try:
        with open(pid_file, encoding="utf-8") as fh:
            pid = int(fh.read().strip())
    except (OSError, ValueError):
        print(json.dumps({"cancelled": False}))
        return 0
    _kill_group(pid)
    print(json.dumps({"cancelled": True}))
    return 0


def op_clean(args: argparse.Namespace) -> int:
    removed: list[str] = []
    if args.older_than_days is not None:
        cutoff = time.time() - args.older_than_days * 86400
        if os.path.isdir(args.root):
            for run in sorted(os.listdir(args.root)):
                run_dir = os.path.join(args.root, run)
                if not NAME.match(run) or not os.path.isdir(run_dir) or os.path.getmtime(run_dir) >= cutoff:
                    continue
                for key in os.listdir(run_dir):
                    if NAME.match(key):
                        _remove_task_dir(os.path.join(run_dir, key), args.checkout)
                shutil.rmtree(run_dir, ignore_errors=True)
                removed.append(run)
    elif args.run:
        run_dir = os.path.join(args.root, _name(args.run, "run id"))
        keys = [args.key] if args.key else (os.listdir(run_dir) if os.path.isdir(run_dir) else [])
        for key in keys:
            task_dir = _task_dir(args.root, args.run, key)
            if os.path.isdir(task_dir):
                _remove_task_dir(task_dir, args.checkout)
                removed.append(f"{args.run}/{key}")
        if os.path.isdir(run_dir) and not os.listdir(run_dir):
            os.rmdir(run_dir)
    else:
        raise Refused("clean needs a run id or --older-than-days")
    print(json.dumps({"removed": removed}))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="orchestrator-node")
    sub = p.add_subparsers(dest="op", required=True)

    def add(name: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name)
        sp.add_argument("--root", required=True)
        sp.add_argument("--checkout")
        return sp

    add("info")
    sp = add("prepare")
    sp.add_argument("run")
    sp.add_argument("key")
    sp.add_argument("base_sha")
    sp.add_argument("--fetch-url")
    sp.add_argument("--fetch-ref")
    sp = add("sync")
    sp.add_argument("run")
    sp.add_argument("key")
    sp = add("run")
    sp.add_argument("run")
    sp.add_argument("key")
    sp.add_argument("job")
    sp.add_argument("--env", action="append", default=[])
    sp = add("cancel")
    sp.add_argument("run")
    sp.add_argument("key")
    sp.add_argument("job")
    sp = add("clean")
    sp.add_argument("run", nargs="?")
    sp.add_argument("key", nargs="?")
    sp.add_argument("--older-than-days", type=float)
    return p


OPS = {
    "info": op_info,
    "prepare": op_prepare,
    "sync": op_sync,
    "run": op_run,
    "cancel": op_cancel,
    "clean": op_clean,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # everything after the first `--` is the command to run, whatever options it holds
    command: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, command = argv[:at], argv[at + 1 :]
    args = parser().parse_args(argv)
    args.argv = command
    if not os.path.isabs(args.root) or (args.checkout and not os.path.isabs(args.checkout)):
        print("orchestrator-node: --root and --checkout must be absolute paths", file=sys.stderr)
        return 2
    try:
        return OPS[args.op](args)
    except Refused as e:
        print(f"orchestrator-node: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
