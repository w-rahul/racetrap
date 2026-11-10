"""Run scenario files as pytest tests, one test per pair: pytest racetrap.toml"""

import warnings

import pytest

# racetrap is imported inside the functions, so a pytest run that uses no
# scenario does not pay for loading the database driver.


class Race(Exception):
    pass


def expected():
    """Errors that mean racetrap could not run. They are shown without a traceback."""
    import psycopg

    from .explore import RunError
    from .scenario import ScenarioError

    return ScenarioError, RunError, psycopg.Error, OSError


def pytest_collect_file(parent, file_path):
    # Only files named on the command line or in testpaths. A scenario empties
    # tables, so installing racetrap must never run one by surprise.
    if file_path.suffix == ".toml" and parent.session.isinitpath(file_path):
        return ScenarioFile.from_parent(parent, path=file_path)


class ScenarioFile(pytest.File):
    result = None

    def collect(self):
        from .scenario import ScenarioError, load

        try:
            self.scenario = load(self.path)
        except ScenarioError as e:
            raise self.CollectError(str(e)) from None
        names = [r.name for r in self.scenario.requests]
        for a in names:
            for b in names:
                yield PairItem.from_parent(self, name=f"{a} vs {b}", pair=(a, b))

    def pairs(self):
        """Run the scenario once, when the first of its tests starts."""
        from .cli import run, run_async
        from .report import render

        if self.result is None:
            try:
                result = run_async(run(self.scenario))
            except expected() as e:
                self.result = e
            else:
                report = self.config.invocation_params.dir / f"racetrap-report-{self.path.stem}.html"
                report.write_text(render(self.scenario, result), encoding="utf-8")
                for w in result.warnings:
                    warnings.warn(pytest.PytestWarning(f"racetrap: {w}"), stacklevel=1)
                self.result = report, {(p.a.name, p.b.name): p for p in result.pairs}
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PairItem(pytest.Item):
    def __init__(self, *, pair, **kwargs):
        super().__init__(**kwargs)
        self.pair = pair

    def runtest(self):
        from .report import explain, passed

        report, pairs = self.parent.pairs()
        pair = pairs[self.pair]
        if pair.skipped:
            pytest.skip(passed(pair))
        if pair.races:
            raise Race(f"{explain(pair)}\nReport: {report}")

    def repr_failure(self, excinfo, style=None):
        if isinstance(excinfo.value, (Race, *expected())):
            return str(excinfo.value)
        return super().repr_failure(excinfo, style)

    def reportinfo(self):
        return self.path, None, f"racetrap: {self.name}"
