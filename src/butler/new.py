"""`butler new` — stand up a butler in a project.

Runs before any butler.toml exists, so it cannot go through the normal
config-loading path. It writes three files and then gets out of the way; the
generated butler.py is never edited again except to bump the harness pin.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import ui
from .config import CONFIG_DIR
from .errors import ButlerError

TEMPLATES = Path(__file__).resolve().parent / "templates"

# Where a generated shim fetches the harness from.
#
# Here that is the private Forgejo instance, reached over SSH with whatever key
# the machine already has. CI, which has a token and no key, rewrites this to
# https with a `git config url.<https>.insteadOf <ssh>` plus an extraheader —
# pip calls git, so both apply. BUTLER_HARNESS_URL overrides it either way.
#
# The published harness carries the public mirror's URL here instead: a reader
# of the mirror cannot reach Forgejo, so a shim pinned there would scaffold
# projects nobody but their author could build. `butler publish` rewrites this
# line on the way out.
DEFAULT_URL = "git+https://github.com/vaelum/butler-harness.git"
DEFAULT_REF = "v0.6.4"

APP_SECTION = """
[app]
kind = "tauri"
dir  = "app"
# Give the dev window its own bundle identity so it can never collide with an
# installed release build's WebView storage or keychain entries.
dev_identifier   = "%%IDENT%%.dev"
dev_product_name = "%%TITLE%% Dev"

# [app.install]              # `app install`: AppImage + icon + .desktop (Linux)
# name         = "%%TITLE%%"
# programs_dir = "/opt/apps"
# comment      = "%%TITLE%%"

[app.android]
key_name  = "%%NAME%%"
cleartext = false   # true to permit plain HTTP to a self-hosted LAN backend
split_abi = false   # true for one APK per ABI (~4x smaller each)
"""

SERVER_SECTION = """
[server]
kind    = "fastapi"
dir     = "server"
module  = "app.main:app"
port    = 8000
compose = true
# db_file = "%%NAME%%.db"   # enables `server reset`

# [server.deploy]
# host       = "user@host"
# dir        = "/srv/%%NAME%%"
# backup_dir = "/srv/backup/%%NAME%%"
# backup     = "zip-data-dir"     # or "sqlite-file" / "none"
"""


def render(path: Path, **subs: str) -> str:
    text = path.read_text()
    for key, value in subs.items():
        text = text.replace(f"%%{key.upper()}%%", value)
    return text


def write(path: Path, content: str, *, force: bool, executable: bool = False,
          label_from: Path | None = None) -> bool:
    label = str(path.relative_to(label_from)) if label_from else path.name
    if path.exists() and not force:
        ui.warn("exists:", f"{label} (left alone; --force to overwrite)")
        return False
    path.write_text(content)
    if executable:
        path.chmod(0o755)
    ui.ok("wrote", label)
    return True


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="butler new",
        description="Scaffold butler.py + butler.toml in a project.")
    p.add_argument("directory", nargs="?", default=".",
                   help="the project root (default: the current directory)")
    p.add_argument("--name", help="project name (default: the directory name)")
    p.add_argument("--app", action="store_true", help="include a Tauri [app] section")
    p.add_argument("--server", action="store_true", help="include a FastAPI [server] section")
    p.add_argument("--tasks", action="store_true", help="also write butler_tasks.py")
    p.add_argument("--flat", action="store_true",
                   help="put butler.toml/butler_tasks.py at the project root "
                        "instead of in butler/")
    p.add_argument("--url", default=DEFAULT_URL, help="harness git URL for the shim")
    p.add_argument("--ref", default=DEFAULT_REF, help="harness ref the shim pins")
    p.add_argument("--force", action="store_true", help="overwrite existing files")
    args = p.parse_args(argv)

    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        raise ButlerError(f"{root} is not a directory")
    # Config and tasks live in butler/, so the project root gains one entry —
    # the shim — instead of three. --flat keeps everything at the root.
    conf_dir = root if args.flat else root / CONFIG_DIR
    conf_dir.mkdir(parents=True, exist_ok=True)
    name = args.name or root.name
    title = name[:1].upper() + name[1:]
    subs = {"name": name, "title": title, "ident": f"com.{name}.app"}

    ui.plain(f"Setting up butler for {ui.bold(name)} in {root}")
    ui.plain()

    write(root / "butler.py",
          render(TEMPLATES / "shim.py", name=name, url=args.url, ref=args.ref),
          force=args.force, executable=True)

    toml = render(TEMPLATES / "butler.toml.tmpl", **subs)
    if args.app:
        toml += _sub(APP_SECTION, subs)
    if args.server:
        toml += _sub(SERVER_SECTION, subs)
    write(conf_dir / "butler.toml", toml, force=args.force, label_from=root)

    if args.tasks:
        write(conf_dir / "butler_tasks.py",
              render(TEMPLATES / "butler_tasks.py.tmpl", **subs), force=args.force,
              label_from=root)

    ui.plain()
    ui.plain("Next:")
    ui.plain("  python butler.py --help      # what the config produced")
    ui.plain("  python butler.py doctor      # what tooling is installed")
    if not (args.app or args.server):
        rel = "butler.toml" if args.flat else f"{CONFIG_DIR}/butler.toml"
        ui.plain(f"  edit {rel:<23}# declare an [app] / [server] component")
    return 0


def _sub(text: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        text = text.replace(f"%%{key.upper()}%%", value)
    return text


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
