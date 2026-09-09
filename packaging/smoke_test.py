"""Check that the packaged executable actually runs.

A build can succeed and still ship a binary that dies on its first import,
or one that serves a 404 for its own interface because the assets were left
out. Neither shows up in the unit tests, which run against the source tree.
So: run the CLI, then start the interface against a throwaway catalog and
ask it for a page and an API response.

Called as: smoke_test.py <path-to-exe>
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TIMEOUT = 60  # generous: a onefile build unpacks itself on every start


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_cli(exe, db):
    out = subprocess.run([exe, "--db", db, "--help"], capture_output=True,
                         text=True, timeout=TIMEOUT)
    if out.returncode != 0:
        raise SystemExit(f"CLI exited {out.returncode}\n{out.stdout}{out.stderr}")
    if "lemon-zest" not in out.stdout.lower():
        raise SystemExit(f"CLI help does not look like ours:\n{out.stdout}")
    print("ok   cli --help")


def get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.read()


def stop(proc):
    """Stop the server and everything it spawned.

    A onefile build is two processes: the bootloader that unpacked the
    program, and the child that is actually running it. Signalling only the
    parent leaves the child alive holding the .exe open, and the next build
    then fails to overwrite it - so take down the whole tree.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_server(exe, db):
    port = free_port()
    proc = subprocess.Popen(
        [exe, "--db", db, "gui", "--port", str(port), "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + TIMEOUT
        while True:
            if proc.poll() is not None:
                raise SystemExit(f"server exited early:\n{proc.stdout.read()}")
            try:
                get(base + "/api/stats")
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                if time.time() > deadline:
                    raise SystemExit("server never came up")
                time.sleep(0.3)

        status, body = get(base + "/api/stats")
        stats = json.loads(body)
        if status != 200 or "tracks" not in stats:
            raise SystemExit(f"unexpected /api/stats: {status} {body[:200]}")
        print("ok   api  /api/stats")

        for path, needle in (("/", b"<title>Lemon Zest</title>"),
                             ("/app.css", b"{"),
                             ("/app.js", b"function")):
            status, body = get(base + path)
            if status != 200 or needle not in body:
                raise SystemExit(f"bad asset {path}: {status}, "
                                 f"{len(body)} bytes")
            print(f"ok   ui   {path} ({len(body)} bytes)")
    finally:
        stop(proc)


def main():
    exe = os.path.abspath(sys.argv[1])
    if not os.path.isfile(exe):
        raise SystemExit(f"no executable at {exe}")

    workdir = tempfile.mkdtemp(prefix="lemon-zest-smoke-")
    db = os.path.join(workdir, "smoke.db")
    try:
        run_cli(exe, db)
        run_server(exe, db)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("smoke test passed")


if __name__ == "__main__":
    main()
