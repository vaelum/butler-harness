# Adding a butler to a project

A walkthrough for a project that has a Tauri app and a FastAPI backend. Skip
whichever half you don't have — every component is optional, and a project with
neither still gets `doctor` and its own tasks.

## 1. Scaffold

From the project root:

```
butler new . --app --server --tasks
```

(If the harness isn't installed anywhere yet, run it out of the checkout:
`python path/to/butler/src/butler/new.py . --app --server`.)

That writes three files:

```
butler.py               # the bootstrap shim — the only thing at the root
butler/butler.toml      # what this project has
butler/butler_tasks.py  # what only this project does (optional)
```

| file | what it is | edit it? |
|---|---|---|
| `butler.py` | the bootstrap shim | only to bump the harness pin |
| `butler/butler.toml` | what this project has | yes, constantly |
| `butler/butler_tasks.py` | what only this project does | when config isn't enough |

Config and tasks sit in `butler/` so the project root gains one entry rather
than three. `--flat` keeps them at the root, and the flat layout is still read
if a project already uses it.

Commit all three. `butler.py` is ~90 lines of stdlib and never grows: it makes
sure a cached venv holding the harness exists, then hands off to it. A fresh
clone on a new machine needs nothing but Python and network access for the
first run.

## 2. Check the ground

```
python butler.py doctor
```

`doctor` reports every tool the declared components need — git, docker, cargo,
`cargo tauri`, the Android SDK/NDK/JDK, build-tools, adb, attached devices, the
signing keystore, the server venv — and says what's missing rather than letting
a build discover it ten minutes in. Run it first on any new machine.

## 3. Describe the project

`butler/butler.toml` is the whole CLI. A command exists only if the config says
the project has that thing, so `--help` is always an accurate description of
what this project can do.

```toml
[project]
name = "acme"
dist = "dist"           # where built installers are collected

[app]
kind = "tauri"
dir  = "app"
# Give the dev window its own bundle identity: a distinct identifier means its
# own WebView data directory and keychain namespace, so it can never collide
# with — or read the credentials of — an installed release build.
dev_identifier   = "com.acme.app.dev"
dev_product_name = "Acme Dev"
# Run before `cargo tauri icon`; must leave app-icon.png behind.
icon_generator   = "gen_icon.py"

[app.install]                            # enables `app install` (Linux)
name         = "Acme"               # must match tauri productName
programs_dir = "/opt/apps"
comment      = "Acme — a self-hosted app"
categories   = ["Utility", "Office"]

[app.android]
key_name    = "acme"     # <vault>/acme/acme.jks
cleartext   = true            # permit plain HTTP to a self-hosted LAN backend
split_abi   = false           # true for one APK per ABI (~4x smaller each)
ndk_version = "26.3.11579264" # prefer this NDK, so local builds match CI

[server]
kind    = "fastapi"
dir     = "server"
module  = "app.main:app"
port    = 8000                # `server run` (uvicorn --reload)
dev_port = 8112               # host port the Compose stack publishes (help text)
compose = true
db_file = "acme.db"      # enables `server reset`

[server.deploy]
host       = "deploy@homeserver.lan"   # or host_file = ".deploy-target" to keep
dir        = "/srv/acme"               # the address out of a public repo
backup_dir = "/srv/backup/acme"
backup     = "zip-data-dir"   # or "sqlite-file" / "none"
build_first = true            # build before stopping, if the image COPYs its source
post_deploy_watch = "Claim token:"   # poll the logs after a FIRST deploy
```

A typo is an error, not a silent no-op: unknown keys are rejected by name, and
so are wrong types. See [components.md](components.md) for every key.

## 4. The commands you now have

```
python butler.py app dev
python butler.py app build --bundles appimage
python butler.py app icon
python butler.py app install
python butler.py app android init|dev|keygen
python butler.py app android build --debug --install --logcat
python butler.py app android install --device SERIAL

python butler.py server run|dev|down|logs|test
python butler.py server deploy
python butler.py server reset

python butler.py doctor
```

Available everywhere: `-n/--dry-run` (print the commands, run nothing),
`-v/--verbose`, `--yes` (skip confirmations, for unattended runs),
`--no-color`. They work before or after the subcommand.

## 5. Android signing, once

```
python butler.py app android keygen
```

This creates a self-signed keystore — fine for sideloading, not a Play Store
upload key — in a vault **outside** the repo: `<vault>/<key_name>/<key_name>.jks`
plus a mode-600 `keystore.properties`. The vault is `$ANDROID_KEYSTORE_VAULT`,
or `~/Documents/important` by default, so one vault signs every app and a fresh
clone of a project contains nothing secret.

**Back the keystore up.** The same key is required to ship in-place upgrades;
losing it means every user must uninstall before they can update.

On a machine without the vault (CI, another laptop), supply the four env vars
instead — `<PREFIX>_ANDROID_KEYSTORE`, `_KS_PASS`, `_KEY_ALIAS`, `_KEY_PASS`,
where the prefix defaults to the project name in upper case. With neither, a
release build still succeeds and produces an **unsigned** APK, with a note
saying so.

## 6. When config isn't enough

Anything the config can't express goes in `butler/butler_tasks.py` and still
appears under `--help` beside the built-ins.

```python
from butler import arg, task, ui
from butler.components import compose


@task("server.claim-token", help="print the first-run claim token")
def claim_token(ctx):
    token = compose.last_log_value(ctx, ctx.cfg.server.dir, "server", "Claim token:")
    if not token:
        ui.warn("no claim token in the logs", "— is the server running and unclaimed?")
        return 1
    ui.ok("claim token:", token)


@task("app.android.build", wraps=True,
      args=[arg("--skip-prep", action="store_true", help="skip the pre-build step")])
def android_build(ctx, args, inner):
    """Wrap the built-in rather than replacing it."""
    if not args.skip_prep:
        ctx.check(["scripts/pre-apk.sh"])
    return inner()
```

Rules of the road:

- The name is a dotted command path. `"node.run"` creates the whole `node`
  branch if the project has no such component — that is how a genuinely
  one-off subsystem gets a home.
- Without `wraps=True` a task **replaces** any built-in at that path. With it,
  the built-in arrives as `inner`.
- Handlers are introspected **by parameter name**: `ctx` is always first; add
  `args` for the parsed flags, `inner` for the wrapped built-in. Declare only
  what you use.
- Return an exit code, or nothing for success.
- Raise `ButlerError(msg, hint=...)` to fail cleanly — the hint is printed
  indented underneath, so put the fixing command there.

What `ctx` gives you: `ctx.run/check/capture/popen/confirm` (echoing, dry-run
aware), `ctx.root`, `ctx.name`, `ctx.dist`, `ctx.cfg`, `ctx.disp(path)` for
display, `ctx.collect(dirs)` to sweep installers into `dist/`.

## 7. Developing the harness itself

Point any project's shim at a working tree — no install, no venv:

```
BUTLER_HARNESS_PATH=/path/to/butler python butler.py app dev
```

Edit the harness, re-run the project's butler, see the change. When it's good,
tag the harness and bump `HARNESS_REF` in the projects that should move.

## 8. Upgrading a project

The harness version is pinned by `HARNESS_REF` in `butler.py` and nothing else.
Bump it, run any command, and the shim builds a new cached venv for that ref —
the old one stays where it is, so other projects on the old pin are unaffected
and rolling back is a one-line edit.
