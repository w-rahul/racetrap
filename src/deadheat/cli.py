import argparse
import asyncio
import http.client
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

from . import __version__
from .explore import Explorer, Recorder, RunError
from .proxy import Proxy
from .report import render
from .scenario import ScenarioError, load


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="deadheat",
        description="Find race conditions in a web API by forcing the worst order of its SQL.",
    )
    parser.add_argument("scenario", help="scenario file in TOML")
    parser.add_argument("--report", default="deadheat-report.html", help="HTML report path (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"deadheat {__version__}")
    args = parser.parse_args(argv)
    try:
        scenario = load(args.scenario)
        host, port = scenario.upstream
        print(f"deadheat {__version__}")
        print(f"proxy  {scenario.proxy[0]}:{scenario.proxy[1]} -> {host}:{port}")
        print(f"app    {scenario.app}\n")
        result = run_async(run(scenario, printer(scenario)))
    except (ScenarioError, RunError, psycopg.Error, OSError) as e:
        print(f"deadheat: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    racing = [p for p in result.pairs if p.races]
    for w in result.warnings:
        print(f"warning: {w}")
    summary = f"{len(racing)} of {len(result.pairs)} pairs race." if racing else f"No races in {len(result.pairs)} pairs."
    print(f"\n{summary} {result.runs} runs in {result.seconds:.1f}s.")
    try:
        Path(args.report).write_text(render(scenario, result), encoding="utf-8")
        print(f"Report: {args.report}")
    except OSError as e:
        print(f"deadheat: could not write the report: {e}", file=sys.stderr)
    return 1 if racing else 0


def printer(scenario):
    width = max(len(r.name) for r in scenario.requests)

    def show(pair):
        verdict = "RACE" if pair.races else "ok"
        orders = f"{len(pair.schedules)} orders"
        print(f"  {pair.a.name:<{width}}  vs  {pair.b.name:<{width}}  {orders:>9}  {verdict}")

    return show


async def run(scenario, progress=None):
    # libpq can hang on an address that drops packets, like ::1 on some Windows
    # setups, so each address gets a deadline unless the URL sets its own
    options = {} if "connect_timeout" in conninfo_to_dict(scenario.database) else {"connect_timeout": 5}
    db = await psycopg.AsyncConnection.connect(scenario.database, autocommit=True, **options)
    try:
        # a lock the app leaves behind should fail the run, not hang it
        await db.execute("SET lock_timeout = '5s'")
        for q in scenario.init:
            await db.execute(q)
        rec = Recorder()
        proxy = Proxy(scenario.proxy, scenario.upstream, rec)
        await proxy.start()
        app = None
        try:
            if scenario.command:
                # an old app left on the port would be tested instead of this one
                if await asyncio.to_thread(ping, scenario.app):
                    raise RunError(f"something already answers at {scenario.app}. Stop it first, deadheat starts the app itself")
                app = start_app(scenario)
            await wait_ready(scenario.app, app)
            alive = (lambda: app[0].poll() is None) if app else None
            return await Explorer(scenario, db, rec, progress, alive).run()
        finally:
            if app:
                stop_app(app)
            await proxy.close()
    finally:
        await db.close()


def run_async(coro):
    # psycopg's async driver cannot use the default Proactor loop on Windows
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


def start_app(scenario):
    env = {**os.environ, scenario.database_env: scenario.proxied_database}
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen(command_args(scenario.command), cwd=scenario.path.parent, env=env, stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def command_args(command):
    if os.name != "nt":
        return shlex.split(command)
    # CreateProcess looks next to the running python.exe before PATH, so a
    # bare "python" would skip the active virtualenv. Resolve it like a shell.
    first, sep, rest = command.partition(" ")
    found = shutil.which(first)
    return f'"{found}"{sep}{rest}' if found and not first.startswith('"') else command


def stop_app(app):
    proc, log = app
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    log.close()


async def wait_ready(url, app, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if app and app[0].poll() is not None:
            raise RunError(f"app exited with code {app[0].returncode}\n{tail(app[1])}")
        if await asyncio.to_thread(ping, url):
            return
        await asyncio.sleep(0.2)
    raise RunError(f"app at {url} did not answer within {timeout}s\n{tail(app[1]) if app else ''}")


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ping(url):
    """True if anything answers HTTP at url, even with an error status."""
    try:
        _opener.open(url, timeout=1).close()
    except urllib.error.HTTPError:
        return True
    except (OSError, http.client.HTTPException):
        return False
    return True


def tail(log, lines=20):
    log.seek(0)
    return "\n".join(log.read().decode("utf-8", "replace").splitlines()[-lines:])
