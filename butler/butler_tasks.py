"""butler's own tasks — the harness managing the harness.

The everyday work on this repository is a test run and a package build, and
both were being typed out by hand with the incantation that makes a src/ layout
work. They are here so that the one entry point (`python3 butler.py`) covers
the whole life of this project: test it, build it, cut a release, export it.

Nothing here belongs in the harness itself. `test` is pytest against a src/
layout, and `build` is a wheel — true of this repository, not of the projects
it serves, which is exactly what butler_tasks.py is for.
"""

from butler import ButlerError, arg, task, ui


@task("test", help="run the test suite",
      args=[arg("pytest_args", nargs="*", metavar="ARG",
                help="passed through to pytest (-k, -x, a test path…)")])
def test(ctx, args):
    # pythonpath = ["src"] in pyproject.toml means pytest needs no install and
    # no venv: it runs against the working tree, the same way this file's own
    # harness is running right now.
    rc = ctx.run(["python3", "-m", "pytest", *args.pytest_args])
    if rc == 0:
        ui.ok("tests passed")
    return rc


def _python(ctx) -> str:
    """The project's dev venv if it has one, else whatever is on PATH.

    `build` is in the dev extra, so a checkout set up the documented way has it
    in .venv and needs nothing installed globally — while a bare machine (CI,
    which makes its own venv) still works.
    """
    venv = ctx.root / ".venv" / "bin" / "python"
    return str(venv) if venv.is_file() else "python3"


@task("build", help="build the wheel and the sdist into dist/")
def build(ctx):
    """What CI publishes on a tag, buildable here before tagging anything.

    `python -m build` makes its own isolated environment for the build
    backend, so this needs nothing installed beyond `build` itself — and it is
    the same command the release job runs, so a packaging mistake surfaces
    here rather than on a tag.
    """
    rc = ctx.run([_python(ctx), "-m", "build", "--outdir", ctx.dist])
    if rc != 0:
        raise ButlerError(
            "the package build failed",
            hint="If `build` itself is missing, it comes with the dev extra:\n"
                 "  python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'")
    for made in sorted(ctx.dist.glob("butler_harness-*")):
        ui.ok("built", ctx.disp(made))
    return 0
