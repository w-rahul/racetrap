import argparse
import asyncio
import contextlib
import copy
import dataclasses
import http.client
import itertools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql as pgsql
from psycopg.conninfo import conninfo_to_dict

from . import __version__, openapi
from .explore import Explorer, Recorder, Result, RunError
from .proxy import Proxy, tls_context
from .report import junit, render, summary
from .scenario import ScenarioError, load


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["init"]:
        return init(argv[1:])
    parser = argparse.ArgumentParser(
        prog="racetrap",
        description="Find race conditions in a web API by forcing the worst order of its SQL.",
        epilog="Start a scenario from an OpenAPI document with: racetrap init --openapi URL",
    )
    parser.add_argument("scenario", help="scenario file in TOML")
    parser.add_argument("--report", default="racetrap-report.html", help="HTML report path (default: %(default)s)")
    parser.add_argument("--json", metavar="PATH", help="also write the results as JSON to this file")
    parser.add_argument("--junit", metavar="PATH", help="also write the results as JUnit XML to this file")
    parser.add_argument("--all-pairs", action="store_true", help="test every pair, even two requests that share no tables")
    parser.add_argument("--workers", type=int, default=1, metavar="N", help="run pairs in parallel, each worker with its own copy of the database and the app")
    parser.add_argument("--version", action="version", version=f"racetrap {__version__}")
    args = parser.parse_args(argv)
    try:
        scenario = load(args.scenario)
        host, port = scenario.upstream
        print(f"racetrap {__version__}")
        print(f"proxy  {scenario.proxy[0]}:{scenario.proxy[1]} -> {host}:{port}")
        print(f"app    {scenario.app}")
        if args.workers > 1:
            print(f"workers {args.workers}")
        print()
        result = run_async(run(scenario, printer(scenario), args.all_pairs, args.workers))
    except (ScenarioError, RunError, psycopg.Error, OSError) as e:
        print(f"racetrap: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130

    racing = [p for p in result.pairs if p.races]
    for w in result.warnings:
        print(f"warning: {w}")
    verdict = f"{len(racing)} of {len(result.pairs)} pairs race." if racing else f"No races in {len(result.pairs)} pairs."
    print(f"\n{verdict} {result.runs} runs in {result.seconds:.1f}s.")
    outputs = [("Report", args.report, lambda: render(scenario, result))]
    if args.json:
        outputs.append(("JSON", args.json, lambda: json.dumps(summary(scenario, result), indent=2) + "\n"))
    if args.junit:
        outputs.append(("JUnit", args.junit, lambda: junit(scenario, result)))
    for label, path, make in outputs:
        try:
            Path(path).write_text(make(), encoding="utf-8")
            print(f"{label}: {path}")
        except OSError as e:
            print(f"racetrap: could not write {path}: {e}", file=sys.stderr)
    return 1 if racing else 0


def init(argv):
    parser = argparse.ArgumentParser(prog="racetrap init", description="Write a starting scenario file from an OpenAPI document.")
    parser.add_argument("--openapi", required=True, help="path or URL of the OpenAPI document, JSON or YAML, like http://127.0.0.1:8000/openapi.json")
    parser.add_argument("--app", help="base URL of the app (default: from the URL or the spec's servers)")
    parser.add_argument("-o", "--output", default="racetrap.toml", help="file to write (default: %(default)s)")
    parser.add_argument("--force", action="store_true", help="overwrite the output file if it exists")
    args = parser.parse_args(argv)
    out = Path(args.output)
    if out.exists() and not args.force:
        print(f"racetrap: {out} already exists. Pass --force to overwrite it.", file=sys.stderr)
        return 2
    try:
        spec = openapi.load_spec(args.openapi)
        text, count = openapi.scenario(spec, args.openapi, args.app or openapi.default_app(spec, args.openapi), out.name)
        out.write_text(text, encoding="utf-8")
    except (openapi.InitError, OSError) as e:
        print(f"racetrap: {e}", file=sys.stderr)
        return 2
    print(f"Wrote {out} with {count} requests. Check the example bodies, then run: racetrap {out}")
    return 0


def printer(scenario):
    width = max(len(r.name) for r in scenario.requests)

    def show(pair):
        verdict = "RACE" if pair.races else "skipped" if pair.skipped else "ok"
        orders = "" if pair.skipped else f"{len(pair.schedules)} orders"
        print(f"  {pair.a.name:<{width}}  vs  {pair.b.name:<{width}}  {orders:>9}  {verdict}")

    return show


async def run(scenario, progress=None, all_pairs=False, workers=1):
    start = time.perf_counter()
    db = await connect(scenario.database)
    try:
        for q in scenario.init:
            await db.execute(q)
        # more workers than pairs would only start apps that have nothing to do
        workers = max(1, min(workers, len(scenario.requests) ** 2))
        if workers == 1:
            return await explore(scenario, db, progress, all_pairs)
        ident, source = pgsql.Identifier, db.info.dbname
        clones = [f"racetrap_{i}_{source}"[:63] for i in range(1, workers)]
        drop = pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
        try:
            for name in clones:
                await db.execute(drop.format(ident(name)))
                try:
                    await db.execute(pgsql.SQL("CREATE DATABASE {} TEMPLATE {}").format(ident(name), ident(source)))
                except psycopg.errors.ObjectInUse as e:
                    raise RunError(f"--workers copies the database, and Postgres only allows that with every other connection to it closed. {e}") from None
            jobs = [partial(worker, s, progress, all_pairs, (i, workers)) for i, s in enumerate(split(scenario, clones))]
            return merge(await in_threads(jobs), start)
        finally:
            for name in clones:
                await db.execute(drop.format(ident(name)))
    finally:
        await db.close()


async def connect(database):
    # libpq can hang on an address that drops packets, like ::1 on some Windows
    # setups, so each address gets a deadline unless the URL sets its own
    options = {} if "connect_timeout" in conninfo_to_dict(database) else {"connect_timeout": 5}
    db = await psycopg.AsyncConnection.connect(database, autocommit=True, **options)
    # a lock the app leaves behind should fail the run, not hang it
    await db.execute("SET lock_timeout = '5s'")
    return db


async def explore(scenario, db, progress, all_pairs, share=(0, 1)):
    rec = Recorder()
    proxy = Proxy(scenario.proxy, scenario.upstream, rec, tls_context(conninfo_to_dict(scenario.database)))
    await proxy.start()
    app = None
    try:
        if scenario.command:
            # an old app left on the port would be tested instead of this one
            if await asyncio.to_thread(ping, scenario.app):
                raise RunError(f"something already answers at {scenario.app}. Stop it first, racetrap starts the app itself")
            app = start_app(scenario, tell_port=share[1] > 1)
        await wait_ready(scenario.app, app)
        alive = (lambda: app[0].poll() is None) if app else None
        return await Explorer(scenario, db, rec, progress, alive, all_pairs, share).run()
    finally:
        if app:
            stop_app(app)
        await proxy.close()


async def worker(scenario, progress, all_pairs, share):
    db = await connect(scenario.database)
    try:
        return await explore(scenario, db, progress, all_pairs, share)
    finally:
        await db.close()


def split(scenario, clones):
    """One scenario per worker. Each has its own database, proxy port and app port."""
    app = urlsplit(scenario.app)
    if not scenario.command or not app.port:
        raise RunError("--workers needs command and a port in app, so racetrap can start one copy of the app per worker")
    database = urlsplit(scenario.database)
    scenarios = []
    for i, clone in enumerate([None, *clones]):
        port = str(app.port + i)
        scenarios.append(
            dataclasses.replace(
                scenario,
                # a login writes its tokens into the requests, and each worker logs in itself
                requests=copy.deepcopy(scenario.requests),
                app=urlunsplit(app._replace(netloc=f"{app.netloc.rpartition(':')[0]}:{port}")),
                command=re.sub(rf"\b{app.port}\b", port, scenario.command),
                proxy=(scenario.proxy[0], scenario.proxy[1] + i),
                database=urlunsplit(database._replace(path="/" + quote(clone, safe=""))) if clone else scenario.database,
            )
        )
    return scenarios


async def in_threads(jobs):
    """Run each job on its own thread and event loop, so no worker delays another's timers."""
    live = []

    def thread(job):
        async def main():
            live.append((asyncio.get_running_loop(), asyncio.current_task()))
            return await job()

        return run_async(main())

    tasks = [asyncio.create_task(asyncio.to_thread(thread, job)) for job in jobs]
    try:
        return await asyncio.gather(*tasks)
    finally:
        # one failure, or Ctrl+C, stops the others, and each one cleans up after itself
        for loop, task in live:
            with contextlib.suppress(RuntimeError):  # that loop has already closed
                loop.call_soon_threadsafe(task.cancel)
        await asyncio.gather(*tasks, return_exceptions=True)


def merge(results, start):
    """What the workers found, with the pairs back in their usual order."""
    pairs = [p for group in itertools.zip_longest(*(r.pairs for r in results)) for p in group if p is not None]
    warnings = list(dict.fromkeys(w for r in results for w in r.warnings))
    ignored = set().union(*(r.ignored for r in results))
    return Result(pairs, ignored, results[0].fks, sum(r.runs for r in results), time.perf_counter() - start, warnings)


def run_async(coro):
    # psycopg's async driver cannot use the default Proactor loop on Windows
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)


def start_app(scenario, tell_port=False):
    env = {**os.environ, scenario.database_env: scenario.proxied_database}
    if tell_port:
        # for apps that take their port from the environment and not from the command
        env["PORT"] = str(urlsplit(scenario.app).port)
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
