"""Turning a project's configuration into an argparse tree, and running it."""

from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

from . import command, components, config, doctor, tasks, ui
from .command import Node
from .context import Ctx
from .errors import ButlerError

PROG = "butler.py"


# --------------------------------------------------------------------------- #
# building the tree
# --------------------------------------------------------------------------- #

def build_tree(cfg: config.Config, project_tasks: list[tasks.Task]) -> list[Node]:
    roots = components.tree(cfg)
    roots.append(doctor.node())
    for name, milestone in sorted(cfg.planned.items()):
        roots.append(_planned(name, milestone))
    overlay(roots, project_tasks)
    roots.sort(key=lambda n: n.name)
    return roots


def _planned(name: str, milestone: str) -> Node:
    """A configured-but-unimplemented section becomes a command that says so,
    rather than vanishing — silence would read as "my config is wrong"."""

    def handler(ctx: Ctx, args) -> int:
        raise ButlerError(
            f"[{name}] is configured, but the '{name}' component is not implemented yet",
            hint=f"Planned: {milestone}")

    return Node(name, f"not implemented yet ({milestone})", func=handler)


def overlay(roots: list[Node], project_tasks: list[tasks.Task]) -> None:
    """Merge butler_tasks.py declarations into the built-in tree."""
    for t in project_tasks:
        node = command.ensure(roots, t.path)
        if t.wraps:
            inner_impl = node.func
            if inner_impl is None:
                raise ButlerError(
                    f"@task('{'.'.join(t.path)}', wraps=True) has nothing to wrap",
                    hint="There is no built-in command at that path. Drop wraps=True.")
            node.func = _wrapper(t.func, inner_impl)
            node.args = [*node.args, *t.args]
        else:
            node.func = t.func
            if t.args:
                node.args = t.args
            if t.help:
                node.help = t.help
        if not node.help:
            node.help = "(project task)"
    # A task path can bring a whole branch into existence ("node.build" in a
    # project with no `node` component). Nothing named that branch, so give it a
    # label rather than letting it sit blank in --help.
    for root in roots:
        if not root.help and root.children:
            root.help = "project commands"


def _wrapper(outer, inner_impl):
    def handler(ctx: Ctx, args):
        return tasks.invoke(outer, ctx, args,
                            inner=lambda: tasks.invoke(inner_impl, ctx, args))

    return handler


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def _global_flags() -> argparse.ArgumentParser:
    """Flags valid at any depth.

    SUPPRESS defaults matter: without them, a subparser's copy of --dry-run
    would write False into the namespace and clobber a --dry-run given before
    the subcommand.
    """
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
                   help="show more about what butler is doing")
    p.add_argument("-n", "--dry-run", action="store_true", default=argparse.SUPPRESS,
                   help="print the commands that would run, without running them")
    p.add_argument("--no-color", action="store_true", default=argparse.SUPPRESS,
                   help="disable coloured output")
    p.add_argument("--yes", action="store_true", default=argparse.SUPPRESS,
                   help="skip confirmation prompts (for unattended runs)")
    return p


def version_report(root: Path | None) -> str:
    """What harness is actually running, and what the project asked for.

    Those two can differ — BUTLER_HARNESS_PATH runs a working tree regardless of
    the pin — and "which butler am I running?" is exactly the question this flag
    exists to answer, so it reports both and says when they disagree.
    """
    import butler

    lines = [f"butler {butler.__version__}"]
    lines.append(f"  running from  {Path(butler.__file__).resolve().parent}")

    dev = os.environ.get("BUTLER_HARNESS_PATH")
    if dev:
        lines.append(f"  source        working tree (BUTLER_HARNESS_PATH={dev})")

    pin = project_pin(root) if root else None
    if pin:
        lines.append(f"  project pin   {pin}")
        if dev:
            lines.append("                (ignored — the working tree above is "
                         "what is running)")
    return "\n".join(lines)


def project_pin(root: Path) -> str | None:
    """The HARNESS_REF the project's shim pins, read without importing it."""
    shim = root / "butler.py"
    if not shim.is_file():
        return None
    for line in shim.read_text().splitlines():
        if line.startswith("HARNESS_REF"):
            parts = line.split('"')
            return parts[1] if len(parts) > 1 else None
    return None


def build_parser(roots: list[Node], project_name: str) -> argparse.ArgumentParser:
    globals_ = _global_flags()
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=f"{project_name} butler",
        parents=[globals_],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Only _func here. `parents=` shares the ACTION OBJECTS with every
    # subparser, and set_defaults mutates action.default in place — so setting
    # defaults for the global flags on the root would give every subparser a
    # concrete False default, which then overwrites a flag given before the
    # subcommand (`butler.py -n app build` silently ran for real). The globals
    # stay SUPPRESS and are filled in after parsing instead.
    parser.set_defaults(_func=None)
    # Listed for --help; handled before parsing, because the subcommand is
    # required and `butler.py --version` must not trip that.
    parser.add_argument("--version", action="store_true",
                        help="show the harness version in use, and exit")
    _add_level(parser, roots, globals_, depth=0, required=True)
    return parser


def _add_level(parser: argparse.ArgumentParser, nodes: list[Node],
               globals_: argparse.ArgumentParser, depth: int, required: bool) -> None:
    """Attach `nodes` as subcommands of `parser`, recursing into their children.

    `required` is False only for a branch that also carries a handler — such a
    node can be invoked bare (that's how a composed `dev` gets to be both a
    command and a namespace).
    """
    if not nodes:
        return
    sub = parser.add_subparsers(dest=f"_level{depth}", metavar="<command>",
                                required=required)
    for node in nodes:
        p = sub.add_parser(node.name, help=node.help, description=node.help,
                           parents=[globals_], epilog=node.epilog,
                           formatter_class=argparse.RawDescriptionHelpFormatter)
        for a in node.args:
            p.add_argument(*a.flags, **a.kwargs)
        if node.func is not None:
            p.set_defaults(_func=node.func)
        _add_level(p, node.children, globals_, depth + 1,
                   required=node.func is None)


def suggest(token: str, roots: list[Node], cfg: config.Config | None = None) -> str | None:
    """Point at the right command when the first word is nearly right.

    This is also the migration aid for the projects whose CLI shape changed.
    Those old CLIs led with a *value* — `butler.py release --build --test` — so
    matching command names is not enough: a preset name has to map back to the
    command that now takes it.
    """
    if cfg is not None and cfg.build is not None and token in cfg.build.presets:
        return f"did you mean:  {PROG} build {token}"
    names = [n.name for n in roots]
    close = difflib.get_close_matches(token, names, n=1, cutoff=0.6)
    if close:
        return f"did you mean:  {PROG} {close[0]}"
    for root in roots:
        if root.child(token) is not None:
            return f"did you mean:  {PROG} {root.name} {token}"
    return None


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Colour has to be decided before the first message, which may well be a
    # config error raised below.
    ui.init("--no-color" in argv)

    # Before anything else: --version must work outside a project too, and must
    # not be blocked by the required subcommand.
    if "--version" in argv:
        try:
            root = (root if root and config.config_path(root)
                    else config.find_root(root))
        except ButlerError:
            root = None
        ui.plain(version_report(root))
        return 0

    try:
        # An explicit root (the shim exports where butler.py lives) wins, but
        # only if it really is a project — otherwise walk up from it, which is
        # what makes running from a subdirectory work.
        project_root = (root if root and config.config_path(root)
                        else config.find_root(root))
        cfg = config.load(project_root)
        tasks.clear()
        project_tasks = tasks.load(project_root)
        roots = build_tree(cfg, project_tasks)
    except ButlerError as e:
        ui.err(e.message, e.hint)
        return e.code

    if argv and not argv[0].startswith("-") and not any(n.name == argv[0] for n in roots):
        hint = suggest(argv[0], roots, cfg)
        if hint:
            ui.err(f"unknown command '{argv[0]}'", hint)
            return 2

    parser = build_parser(roots, cfg.project.name)
    args = parser.parse_args(argv)
    for flag in ("verbose", "dry_run", "no_color", "yes"):
        setattr(args, flag, getattr(args, flag, False))
    ui.init(args.no_color)

    if args._func is None:
        parser.print_help()
        return 2

    ctx = Ctx(cfg=cfg, dry_run=args.dry_run, verbose=args.verbose,
              assume_yes=args.yes, args=args)
    try:
        return tasks.invoke(args._func, ctx, args)
    except ButlerError as e:
        ui.err(e.message, e.hint)
        return e.code
    except KeyboardInterrupt:
        ui.plain()
        return 130
