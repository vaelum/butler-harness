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
`publish` (Copybara export to a public mirror), `release` (squash, tag, wait
for the build, export), plus `doctor`.

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
butler.py release 1.2.3 --check
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

The harness runs on itself. The `butler.py` in this repository is not a
bootstrap shim — it runs `src/` directly, because a harness that pinned a
release of itself could not ship the fix to its own release step:

```
python3 butler.py test          # pytest against src/, no install
python3 butler.py build         # the wheel and sdist a release attaches
python3 butler.py publish check # what the mirror would and would not get
python3 butler.py release 0.8.1 # squash, tag, wait for CI, export, release
```

`release` is the whole cycle: it squashes `dev` into `main` as one commit, tags
it, pushes to Forgejo, waits for the build that publishes the wheel and the
sdist as a Forgejo release, then exports the tag to the mirror and copies that
release — assets, notes and all — onto GitHub. A build that fails has published
nothing, so the same version can be re-cut with `--retag` once it is fixed.
