import asyncio
import sys


def run_async(coro):
    # psycopg's async driver cannot use the default Proactor loop on Windows
    factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        return runner.run(coro)
