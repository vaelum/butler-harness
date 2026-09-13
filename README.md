# butler

A shared harness for the per-project `butler.py` scripts — one CLI grammar, one
implementation of the machinery (Tauri builds, Android signing/sideloading,
Docker Compose, SSH deploys, CMake/CTest, MegaLinter), and a documented recipe
for adding a butler to a new project.

Stdlib-only, MIT licensed. A project keeps a ~90-line shim that pins a version
of this repo and bootstraps it into a cached virtualenv on first use, so a fresh
clone needs nothing installed beyond Python.

## Status

**[chords](https://github.com/vaelum/chords) runs on the harness fully** — its
`butler.py` went from 735 hand-rolled lines to a 96-line shim, with everything
it declares in `butler/butler.toml` and its handful of project-specific tasks in
`butler/butler_tasks.py`. Nothing of the old script remains, and as of 0.4.0 its
`scripts/deploy.sh` is gone too — it deploys through the built-in. It is the
reference for what a migrated project looks like.

Implemented components: `app` (Tauri desktop + Android), `server` (FastAPI,
Compose or the project's own scripts), `extension` (Chrome + Firefox bundles),
`build` (CMake + Docker buildenv), `check` (MegaLinter + clang-tidy),
`publish` (Copybara export to a public mirror), plus `doctor`.

- [docs/new-project.md](docs/new-project.md) — adding a butler to a project
- [docs/components.md](docs/components.md) — every config key and command

## The shape of it

A consuming project holds three files:

```
butler.py               # committed shim; bootstraps a cached venv, then hands off
butler/butler.toml      # declares which components the project has
butler/butler_tasks.py  # optional: bespoke tasks and overrides
```

and gets the same CLI everywhere:

```
butler.py app dev
butler.py app build --bundles appimage
butler.py app android build --debug --install --logcat
butler.py server dev | down | logs | test | deploy
butler.py extension package
butler.py doctor
```

Commands appear only when the config declares that component, so `--help` is
always an accurate description of the project.

## Developing the harness

Point any project's shim at a working tree instead of the cached venv — no
install, no venv, changes visible immediately:

```
BUTLER_HARNESS_PATH=/path/to/butler python butler.py app dev
```

Tests run against `src/` directly:

```
python -m pytest
```
