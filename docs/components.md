# Component reference

Every section of `butler.toml` is optional. A component's commands exist only
when its section is present, so `python butler.py --help` is always an accurate
description of the project.

Unknown keys and wrong types are errors naming the offending key — a typo never
silently does nothing.

---

## `[project]`

| key | type | default | meaning |
|---|---|---|---|
| `name` | string | **required** | used for artifact names, the keystore folder, backup filenames, env-var prefixes |
| `dist` | string | `"dist"` | where built installers are collected |
| `submodules` | bool | `false` | update git submodules before a build |
| `git_deps` | array of tables | `[]` | non-submodule checkouts: `{ path, url, ref }` |

---

## `[app]` — Tauri

| key | type | default | meaning |
|---|---|---|---|
| `kind` | string | `"tauri"` | only `tauri` so far |
| `dir` | string | `"app"` | the Tauri project, relative to the root; `src-tauri` is assumed inside it |
| `dev_identifier` | string | — | bundle identifier for `app dev` |
| `dev_product_name` | string | — | window/product name for `app dev` |
| `icon_generator` | string | — | script run before `cargo tauri icon`; must produce `app-icon.png` |
| `icon_source` | string | `app/app-icon.png` | master art for `cargo tauri icon`, relative to the **project** root — it is often shared with a web frontend and lives outside the app dir |

`dev_identifier` is worth setting. A distinct identifier gives the dev window
its own WebView data directory and keychain namespace, so it cannot collide
with — or read the credentials of — an installed release build.

**Commands**

| command | notes |
|---|---|
| `app dev` | `cargo tauri dev`, under the dev identity when configured |
| `app build [--debug] [--no-bundle] [--bundles KIND...]` | bundles land in `dist/` |
| `app icon` | runs `icon_generator`, then `cargo tauri icon` |
| `app install` | only when `[app.install]` is set |

Linux bundle builds run with `APPIMAGE_EXTRACT_AND_RUN=1` and `NO_STRIP=1`:
AppImage's linuxdeploy is itself an AppImage and needs FUSE, which headless,
containerised and hardened hosts don't have.

---

## `[app.install]` — Linux desktop install

| key | type | default | meaning |
|---|---|---|---|
| `name` | string | **required** | install name; match tauri `productName` |
| `programs_dir` | string | **required** | where the AppImage and its icon live |
| `comment` | string | `""` | `.desktop` Comment |
| `categories` | list | `["Utility"]` | `.desktop` Categories |
| `wm_class` | string | `name` | `StartupWMClass` |

`app install` copies the newest `dist/*.AppImage` to
`<programs_dir>/<name>.AppImage`, the icon beside it as `<name>.png`, and writes
`~/.local/share/applications/<name>.desktop` pointing at both by absolute path.
The names are stable and version-less, so installing a newer build over the old
one leaves the launcher valid.

---

## `[app.android]`

| key | type | default | meaning |
|---|---|---|---|
| `key_name` | string | project name | keystore folder and file name; also the APK name prefix |
| `dname` | string | derived | X.500 name for `keygen` |
| `vault` | string | `"~/Documents/important"` | keystore vault root; `$ANDROID_KEYSTORE_VAULT` overrides |
| `cleartext` | bool | `false` | permit plain-HTTP to a self-hosted LAN backend |
| `split_abi` | bool | `false` | one APK per ABI instead of one universal APK |
| `ndk_version` | string | — | preferred NDK, so a local build matches CI |
| `env_prefix` | string | `NAME` upper-cased | prefix for the four signing env vars |

**Commands**

| command | notes |
|---|---|
| `android init` | scaffolds `gen/android`; re-runnable |
| `android dev` | prepares the gen tree, then `cargo tauri android dev` |
| `android build [--debug] [--no-sign] [--split-abi] [--universal] [--install …]` | |
| `android keygen [--password P]` | one-time; random password by default |
| `android install [--device S] [--reinstall] [--logcat]` | sideloads the newest APK in `dist/` |

### What `build` does, in order

1. resolve the toolchain (SDK, NDK, JDK) or fail with everything that's missing;
2. `android init` if `gen/android` isn't there;
3. copy `src-tauri/icons/android/` over the template's stock artwork — `init`
   seeds the res tree once and never re-reads it, so without this the APK ships
   the wrong icon;
4. write `network_security_config.xml` and wire it into the manifest, when
   `cleartext = true`;
5. **delete `app/build/outputs`** — Gradle leaves an APK behind for every
   variant ever built and the signing step globs the whole tree, so a stale
   universal APK would be re-signed with today's label and shipped as if it were
   this build;
6. `cargo tauri android build --apk`, `--split-per-abi` when asked;
7. debug APKs are already signed with the Android debug key; release APKs come
   out unsigned, so zipalign + apksigner them into
   `<key_name>[-<abi>]-<label>.apk`, where the label is the exact git tag on
   HEAD or a UTC timestamp;
8. copy into `dist/`, and sideload if `--install`.

Nothing here edits the generated Gradle. `gen/android` stays regenerable:
steps 3–5 are idempotent and re-applied before every build, and signing is a
post-build step.

The signing passwords reach apksigner through the environment (`--ks-pass env:`),
never as `pass:` on the command line: butler echoes every command it runs, so an
argv-borne password would be printed into terminals and CI logs, and readable
from `/proc` for as long as apksigner runs.

### Cleartext

`cleartext = true` writes a network security config rather than flipping
`usesCleartextTraffic`. On API 24+ the config takes precedence **and** keeps the
system trust anchors, so ordinary HTTPS is unaffected. Add `<domain>` entries to
the generated file's template if you'd rather not allow cleartext globally.

### Sideloading

`--install` picks the APK to install by asking the device for
`ro.product.cpu.abi` and matching it against the split APKs' arch tokens
(a split build leaves five APKs behind and only two will run on any given
device; the matching split is ~4x smaller). It falls back to the universal APK,
then the newest.

Unsigned and aligned intermediates are never install candidates. Swapping
between a debug and a release build trips `INSTALL_FAILED_UPDATE_INCOMPATIBLE`
because the signing keys differ; butler explains that this needs a clean install
and that a clean install **wipes the app's on-device data**, then asks.
`--reinstall` skips the question.

---

## `[server]` — FastAPI

| key | type | default | meaning |
|---|---|---|---|
| `kind` | string | `"fastapi"` | only `fastapi` so far |
| `dir` | string | `"server"` | the server project |
| `module` | string | `"app.main:app"` | uvicorn target |
| `port` | int | `8000` | port for `server run` |
| `dev_port` | int | — | host port the Compose stack publishes (help text only) |
| `service` | string | `"server"` | Compose service name |
| `compose` | bool | `true` | whether there's a Compose stack |
| `db_file` | string | — | sqlite file inside the data dir; enables `server reset` |
| `data_dir` | string | `"data"` | data directory inside `dir` |
| `compose_file` | string | — | Compose file relative to `dir`, when it isn't at the root |
| `venv` | string | `".venv"` | project venv; `""` to always use the current interpreter |
| `test` | list | `["-m", "pytest"]` | test command, run with the venv's python |

butler does **not** create the venv. The dependency set is the project's
business, and silently building one hides a missing install until something
imports differently in CI. `doctor` tells you it's absent and prints the command.

**Commands:** `run`, `dev [-d]`, `down`, `logs [--no-follow]`, `test [extra…]`,
`deploy`, `reset`. The Compose ones appear only when `compose = true`; `reset`
also needs `db_file`.

`server reset` wipes local data and un-claims the instance, so it demands the
word `wipe` rather than a y/N — a mistyped `y` shouldn't be able to do that.

---

## `[server.scripts]` — delegating to the project's own scripts

Not every server is compose-and-rsync. When a project already drives its stack
with shell scripts, re-expressing that in config would be a rewrite rather than
a migration — so butler delegates and just owns the CLI.

```toml
[server.scripts]
dev    = ["bash", "scripts/build.sh", "dev"]
build  = { cmd = ["bash", "scripts/build.sh"], help = "production stack (Caddy + TLS)" }
```

Each key becomes a `server <name>` command, run with the server directory as
cwd. A value is either an argv array, or a table with `cmd` and an optional
`help` (the default help is `run <cmd>`).

Scripts are applied last and **replace** a same-named built-in — a project that
says how to do something has said so on purpose. Other built-ins are untouched,
so a scripted `dev` can sit beside a Compose `down`.

A `butler_tasks.py` task can still wrap a scripted action, the same way it wraps
a built-in — which is how chords ships its API key to the remote ahead of
`server deploy`.

---

## `[extension]` — browser extension

| key | type | default | meaning |
|---|---|---|---|
| `dir` | string | `"extension"` | the extension source tree |
| `artifact` | string | `"<project>-extension"` | base name of the produced zips |

| `[extension.firefox]` | type | default | meaning |
|---|---|---|---|
| `id` | string | **required** | stable gecko add-on id (email-style ids are valid) |
| `min_version` | string | `"115.0"` | `strict_min_version` |

**`extension package [--label L]`** writes
`dist/<artifact>-chrome-<label>.zip` and, when `[extension.firefox]` is present,
`dist/<artifact>-firefox-<label>.zip`. The label defaults to the git tag on HEAD
or a UTC timestamp; CI passes it explicitly, since `git describe` in a shallow
checkout can't be trusted.

One source tree produces both. Rather than maintaining a second manifest that
will drift, the Firefox manifest is **derived** from the Chrome one at package
time, changing only the two things Firefox needs and Chrome rejects:

- `background` becomes an event page (`{"scripts": ["background.js"]}`) instead
  of a service worker. The same `background.js` ships unchanged — it already
  has to register listeners at top level and persist state to survive a
  service-worker restart, which is exactly what an event page needs too;
- `browser_specific_settings.gecko` carries the stable add-on id and minimum
  version.

Permissions, icons, action and options_page are identical and are never
duplicated. The source manifest on disk is never modified. Zip entries are
written in sorted order, so two runs over the same source produce byte-identical
archives.

---

## `[server.deploy]`

| key | type | default | meaning |
|---|---|---|---|
| `dir` | string | **required** | deploy directory on the host |
| `host` | string | one of the two | ssh target |
| `host_file` | string | one of the two | file holding the ssh target, relative to the server dir |
| `backup_dir` | string | required unless `backup = "none"` | where snapshots land |
| `backup` | string | `"zip-data-dir"` | `zip-data-dir`, `sqlite-file`, or `none` |
| `data_dir` | string | from `[server]` | data directory on the host; absolute or `~`-rooted if it lives outside `dir` |
| `db_file` | string | from `[server]` | required for `sqlite-file` |
| `compose_file` | string | from `[server]` | Compose file relative to `dir` |
| `build_first` | bool | `false` | build before stopping the old stack (see below) |
| `build_env` | list | `[]` | local env vars forwarded to the remote build |
| `exclude` | list | see below | rsync excludes |
| `post_deploy_watch` | string | — | log prefix to poll for after a first deploy |

Default excludes: `data/`, `.env`, `.venv/`, `__pycache__/`, `*.egg-info/`,
`.pytest_cache/`. These matter — the rsync uses `--delete`, so without them a
deploy would remove the live data directory and the production `.env`. A
`host_file` is excluded automatically.

**`server deploy`, in order**

1. probe for the data directory; absent means first deploy. `test` exits 1 for
   "absent" and >1 for ssh failing, and the two are not conflated — treating an
   unreachable host as a first deploy would skip the backup;
2. preflight (is `zip` installed?) **and then** stop the stack — finding out the
   backup tool is missing after taking the service down is a bad trade;
3. snapshot. `zip-data-dir` zips the whole data directory, which is a genuine
   restore point: stop, replace `data/` with the zip's contents, start, and the
   instance is exactly what it was. `sqlite-file` copies the DB plus its
   `-wal`/`-shm` sidecars so the copy is internally consistent;
4. rsync the code in, then `mkdir -p` the data directory — a bind mount whose
   source is missing gets created by the daemon as root, and the service then
   cannot write to its own data;
5. `docker compose up -d --build`;
6. on a first deploy only, poll the logs for `post_deploy_watch` and print what
   follows it — this is how the one-time claim token these servers emit on first
   boot gets surfaced without reading container logs by hand.

**`host` vs `host_file`.** Exactly one is required. `butler.toml` is committed,
and a server address is precisely what a public repo should not carry, so
`host_file = ".deploy-target"` reads it from a gitignored file at deploy time —
late enough that `--help` and `doctor` still work in a fresh clone that hasn't
got one. A missing or empty file is an error that says how to create it.

**`build_first = true`** reorders the middle of that list to `rsync → build →
stop → snapshot → start`, so the image builds while the old container is still
serving and downtime is the swap rather than the build. Where the build pulls a
Node toolchain or a Playwright browser that is minutes of outage saved. It is
only correct when the production image **COPYs** its source in: with a
bind-mounted source tree the running container would pick up the new code at
rsync time, ahead of the swap. The final `up` then doesn't pass `--build`, which
would put the slow part back inside the window.

**`build_env`** forwards named variables from the local environment to the
remote build command — `build_env = ["APT_MIRROR"]` turns
`APT_MIRROR=… butler.py server deploy` into `APT_MIRROR=… docker compose build`
on the host, for build-time knobs like an apt mirror. Only variables that are
actually set are passed. Not for secrets: the script is echoed.

Shell scripts go to the host over `ssh -T host sh -s` with the script on stdin,
never as an ssh command string: the remote login shell is whatever the user set
(fish, here), and it would try to parse POSIX syntax. This way the login shell
only ever sees the two words `sh -s`.

---

## `[build]` — CMake

```
butler.py build [preset] [--test] [--test-filter RE] [--docker] [options…]
```

| key | type | default | meaning |
|---|---|---|---|
| `kind` | string | `"cmake"` | only `cmake` so far |
| `dir` | string | `"."` | where `CMakePresets.json` lives, relative to the root |
| `presets` | array | `["release", "debug"]` | the presets this project declares; the first is the default |
| `build_dir` | string | `"builds/{preset}"` | must contain `{preset}` |
| `cmake_min` | string | `"3.22"` | the minimum CMake this project needs |
| `jobs` | int | `8` | `--parallel` / `ctest -j` |
| `prepare` | string | — | script run before configuring, if it exists; may contain `{preset}` |

`prepare` is for the per-target setup a build needs before CMake can run
(installing system packages, fetching a toolchain). A missing script is not an
error — most presets have none, and the ones that do share the path template.

`preset` is a positional with `choices` from `presets`, so an undeclared one is
rejected by the parser rather than by CMake several seconds later.

`build_dir` is required to be per-preset. Sharing one directory between a debug
and a release build means sharing a CMake cache, which silently reconfigures the
tree back and forth.

Before configuring, `[project] submodules` and `git_deps` are honoured — a stale
submodule is a compile error a long way from its cause.

With `--test`, ctest writes JUnit to `<build_dir>/test-results.xml` (CI reads it
from there), and `--test-filter` is forwarded as `ctest -R`. The path handed to
`--output-junit` is always **absolute** — the project root on the host, the
buildenv's `workdir` under `--docker` — because `ctest --preset` runs in the
preset's binary directory, so a relative one would be resolved against that and
land a level deeper than anything looks for.

### `[[build.option]]` — a build switch

An array of tables. Each becomes one CLI flag and a set of CMake cache variables:

```toml
[[build.option]]
name    = "storage"
cmake   = ["VL_WITH_SFTP", "VL_WITH_S3"]
default = true
help    = "the SFTP/S3 storage clients"
```

The flag polarity follows the default, so it always reads as the thing it
changes: `--no-storage` for a default-on option, `--storage` for a default-off
one. The `-D…=ON/OFF` values are passed on **every** configure, not just the
first, so a build directory previously configured the other way is corrected
rather than silently reused.

### `[build.docker]` — the buildenv container

| key | type | default | meaning |
|---|---|---|---|
| `dockerfile` | string | **required** | relative to the root; may contain `{preset}` |
| `context` | string | the Dockerfile's directory | not the repo root — that would send gigabytes of build output to the daemon |
| `tag` | string | `"<project>-buildenv"` | the image tag; may contain `{preset}` |
| `workdir` | string | `"/src"` | where the tree is mounted inside the container |
| `presets` | array | all of them | presets that *can* build in a container |
| `always` | array | `[]` | presets that build in the container without `--docker` |

`--docker` builds this image (passing `USER_ID`/`GROUP_ID` so artifacts belong to
whoever ran it) and runs the whole configure/build/test sequence inside it as one
`sh -c` script. Nothing but Docker is needed on the host.

`{preset}` in `dockerfile`/`tag`/`context` gives one image per preset — a distro
image per target rather than one image for everything. `always` is for a preset
whose image *is* its toolchain: a host build of it was never a meaningful thing
to ask for, so it doesn't need a flag to say so. `presets` is the other side of
that: asking for `--docker` on a preset with no image is an error naming the ones
that have one, rather than a confusing missing-Dockerfile failure.

A host build and a `--docker` build **cannot share a build directory**: the CMake
cache records absolute paths, and the container sees the tree at `workdir`. Use
`--docker` in CI or a clean checkout.

### `[build.test_env]` — services the tests talk to

| key | type | default | meaning |
|---|---|---|---|
| `up` | array | **required** | command that starts the services, or an array of such commands |
| `down` | array | `[]` | teardown, run in a `finally` |
| `env` | table | `{}` | variables set for the test run |
| `needs` | string | — | a `[[build.option]]` name; skip all of this when that option is off |
| `requires` | array | `[]` | paths that must exist before a test run |
| `docker_args` | array | `[]` | extra `docker run` arguments for a containerised test run |

`up` and `down` take either one command (`["docker", "compose", "up"]`) or
several (`[[…], […]]`). Standing services up is often two steps: start the stack,
then run the job that creates the bucket to completion as a readiness barrier
before any test connects.

Teardown is best-effort and deliberately does not affect the exit code: a compose
file that fails to come down must not turn a red test suite green, or a green one
red.

`requires` is checked before anything starts. Both real cases fail deep and
unrecognisably otherwise: a compose file a *dependency* supplies (absent until
that dependency is cloned), and `/dev/fuse`, whose absence used to silently drop
a container's FUSE flags and surface as dozens of opaque permission-denied mount
failures.

`docker_args` covers what a containerised test run needs and a build does not:
host networking to reach services published on 127.0.0.1, or the device and
capabilities a FUSE mount needs. None of it is applied to a build without
`--test` — nothing is brought up, so nothing needs reaching.

`needs` exists because integration tests only exist when the clients that talk to
these services were compiled in. With the option off there is nothing to
integration-test, so nothing is started.

---

## `[check]` — static analysis

```
butler.py check                       # every configured checker, then the report
butler.py check lint [--fix] [--linters KEY1,KEY2]
butler.py check tidy [options…] [-- extra args]
butler.py check report                # rebuild the report from current results
```

Bare `check` runs every configured checker and keeps going after a failure — the
point of running them together is one list of everything to fix. The worst exit
code wins.

`[check] report = [...]` names a command run after the stages, always, including
when a stage reported findings — a findings report is most useful exactly then.
Each stage refreshes only its own artifact, so the report folds in the latest of
each, which is what makes a lint-only run still show the last tidy results. A
leading `python` means the interpreter butler is running under, not whatever is
on `PATH`. It is also available on its own as `check report`.

### `[check.lint]` — MegaLinter

| key | type | default | meaning |
|---|---|---|---|
| `config` | string | `".mega-linter.yml"` | relative to the root; also the in-container path |
| `image` | string | `"<project>-lint-env"` | image to run |
| `dockerfile` | string | — | build `image` from this first; omit to use a pinned upstream image directly |
| `context` | string | the Dockerfile's directory | only meaningful with `dockerfile` |
| `reports` | string | `"megalinter-reports"` | where the run leaves its artifacts |
| `clear` | bool | `false` | empty that directory before the run |
| `keep` | array | `[]` | entries `clear` preserves |
| `hide` | array | `[]` | trees to overlay with an empty tmpfs |
| `workspace` | string | `"/tmp/lint"` | MegaLinter's conventional mount point |

A non-default `reports` is also passed to MegaLinter as `REPORT_OUTPUT_FOLDER`,
so the log, the SARIF and the per-linter output land there too.

`clear` exists because MegaLinter reuses that directory in place, and the
project-scope scanners walk the workspace *including* it — so last run's SARIF is
read back and re-counted, and a finding that was actually fixed keeps
resurfacing. `keep` is for another stage's artifact living in the same directory
(`clang-tidy.json`), which a lint-only run must not delete.

The container runs as the invoking user, so reports and `--fix` rewrites stay
owned by them, with `HOME=/tmp` and `RUFF_CACHE_DIR=/tmp/.ruff_cache` — ruff
ignores `HOME` and would otherwise cache into the mounted repo.

`hide` is not the same as MegaLinter's own file filtering. Project-scope scanners
(trufflehog, checkov, grype, jscpd) walk the workspace themselves, so build output
and vendored dependencies have to be made *empty*, not merely excluded. Only trees
that exist are overlaid, so linting never creates phantom directories. Don't hide
`.git` — gitleaks scans history there.

The reports directory is created up front as the invoking user: a run of the raw
image without `--user` leaves a root-owned one behind, and failing here names the
problem instead of producing an opaque in-container `PermissionError`.

### `[check.tidy]` — clang-tidy

| key | type | default | meaning |
|---|---|---|---|
| `driver` | string | **required** | script run as `python <driver> <compile-db> [args…]` |
| `preset` | string | the build's default preset | which build to read the database from |
| `db` | string | `"compile_commands.json"` | name of the compile database |
| `docker` | bool | `false` | run the driver inside that preset's buildenv |
| `args` | array | `[]` | extra arguments always passed to the driver |

Set `docker` when the database was produced in a container: it references that
container's toolchain and headers, so the host's clang-tidy would be reading
include paths that only exist inside the image.

`check tidy` builds first (no tests) to generate or refresh the compile database,
then runs the driver over it. The build options are exposed as flags here too and
forwarded to that build: tidying a tree configured with a feature off, using a
database that had it on, reports on code the build never compiled.

A driver rather than a direct `clang-tidy` invocation because every project needs
to filter vendored and third-party entries out of the database first.

## `[publish]` — export to a public mirror

Exports a branch of the private Forgejo repository to a generated GitHub
mirror, through Copybara. **The mirror is generated: never commit to it
directly.** Public commits get new SHAs, each stamped with a
`GitOrigin-RevId` trailer naming the private commit it came from.

```toml
[publish]
forgejo    = "acme/widget"        # the private source
host       = "git.example.org"    # your Forgejo host — required, never defaulted
github     = "acme/widget"        # the mirror; defaults to the same path
visibility = "public"             # "public" | "private" — documents intent
branch     = "main"               # the publication branch
author     = "dev <dev@example.org>"
exclude    = ["CHANGELOG.md"]     # project-specific additions to the baseline
release    = true                 # a tag's CI-built release is copied to the mirror
```

| key | type | default | meaning |
| --- | --- | --- | --- |
| `forgejo` | string | **required** | `owner/name` of the private repository |
| `github` | string | `forgejo` | `owner/name` of the mirror; set it only when the two differ |
| `visibility` | string | `private` | `public` or `private`; `check` warns about a public export that holds nothing back |
| `branch` | string | `main` | the publication branch — development happens elsewhere and never leaves |
| `author` | string | **required** | every exported commit is authored by this identity, whatever the private commit said |
| `committer` | string | `author` | pinned separately only when the two must differ |
| `exclude` | list | `[]` | paths that stay private, **added to** the baseline below |
| `host` | string | **required** | your Forgejo host. Deliberately has no default: one would bake a single instance into a published, reusable harness |
| `port` | int | `2222` | Forgejo SSH port |
| `rewrite_harness` | bool | `true` | repoint `butler.py`'s pin at the public harness on the way out |
| `harness_github` | string | `vaelum/butler-harness` | where the public harness lives |
| `release` | bool | `false` | a tag's release on Forgejo is copied to the mirror — see below. Off by default: most mirrors are plain source, and looking for a release that was never built would fail every tagged export |
| `harness_forgejo` | string | unset | `owner/name` of the harness on your Forgejo. Set it only if your docs link there — the harness is the one repository whose mirror is renamed, so its link cannot be mapped path-for-path |

| command | what it does |
| --- | --- |
| `publish` | export the branch to the mirror |
| `publish --tag v1.2.3` | export, then publish a tag on its corresponding public commit — and, with `release = true`, copy its release |
| `publish --init` | first export into an **empty** mirror; run once |
| `publish --rehearse` | export into a scratch repo and print the tree; pushes nothing |
| `publish check` | list what would and would not be published; touches no network |

### The exclude list is stated once

Every project's export holds back this baseline, and `exclude` adds to it — it
cannot remove from it:

```
*-PLAN.md        infrastructure plans: hosts, addresses, credential paths
**/*-PLAN.md
.forgejo/**      CI: self-hosted runner labels, secret names, internal URLs
copy.bara.sky    the export machinery, where a project still has one
push-public.sh
```

`butler/butler.toml` is deliberately **not** excluded: how a project is built is
part of what a published project tells you.

Before this component each project hand-wrote its own `copy.bara.sky` and
`push-public.sh`, which between them stated the exclude list twice — once as a
glob for Copybara, once as a regex for the tag-equivalence check — under a
comment reading *"keep the two in sync"*. Here the globs are the only source and
the regex is derived from them, so the two cannot drift. An exclude pattern
using syntax the deriver does not support is an error rather than a pattern that
quietly matches nothing.

### The harness pin

A generated `butler.py` pins the harness at
`git+https://github.com/vaelum/butler-harness.git`, which nobody outside can
reach — so a mirror shipping that shim is not buildable by anyone but its
author. With `rewrite_harness` on, the exported copy is repointed at the public
harness, and Forgejo web links in Markdown become their GitHub equivalents —
`harness_forgejo` first if set, since that mirror is renamed, then the rest
path-for-path. Your own checkout is untouched,
so `butler new` run by you still generates a private pin.

### Releases are copied, not rebuilt

The installers are built where the code is private: a `v*` tag pushed to Forgejo
runs the project's CI, which publishes a Forgejo release with every artifact
attached. The mirror has no CI of its own — it is generated, and `.forgejo/**`
is one of the things the export holds back — so `release = true` makes
`publish --tag` *copy* that release onto the public tag: assets, notes, title
and the pre-release flag.

```
git push origin main v1.2.3      # then wait for the build to publish its release
butler publish --tag v1.2.3
```

The order is the design. Everything the release needs is fetched and checked
**before** the export pushes anything, so each of these stops the run with
nothing published rather than leaving a public tag whose release never arrives:

* the tag is not on Forgejo, or names another commit there than it does locally;
* its build has not published a release yet, or published a draft;
* the release has no assets, or one downloads short of the size it lists.

A run that failed partway is simply repeated. A tag already published on the
right commit is left alone, and a GitHub release that already exists gets only
the assets it is missing.

Reading the release needs a Forgejo token with `read:repository`, and creating
the GitHub release needs `gh`, logged in. The token is looked for in the
environment and then in `~/.secrets` (`SECRETS_FILE` overrides the path), under
`FORGEJO_TOKEN_READONLY`, `FORGEJO_TOKEN_FOR_<HOST>` and `FORGEJO_TOKEN`, in
that order — the narrowest first, because this only ever reads. A run says which
name it used and never what it held.

### Rehearse before the first real export

`publish --rehearse` runs the whole export into a scratch bare repository and
prints the resulting tree. It is the only thing standing between an incomplete
exclude list and a permanent public commit, and it costs one command. Setting
`PUBLIC_URL` has the same effect, pointed wherever you like.

## `[release]` — cut a version and take it out

Does the whole cycle in one command: put the work branch on the publication
branch as **one** commit, tag it, push to Forgejo, wait for the build that
publishes the release, then hand over to `publish` for the export, the public
tag and the copy of that release onto the mirror.

```toml
[release]
source        = "dev"               # the branch the work happens on
into          = "main"              # the branch a release is merged into
tag_prefix    = "v"                 # version 1.2.3 is tagged v1.2.3
message       = "{project} {version}"   # subject of the squash commit and the tag
changelog     = "CHANGELOG.md"      # where this version's notes live; "" to skip
version_files = ["pyproject.toml"]  # files whose version must match the tag
timeout       = 3600                # seconds to wait for the build
```

| key | type | default | meaning |
| --- | --- | --- | --- |
| `source` | string | `dev` | the branch the work happens on; it is never rewritten |
| `into` | string | `[publish] branch` | the publication branch. Must be the exported one — a release merged anywhere else never reaches the mirror, so stating a different branch is an error rather than a surprise |
| `tag_prefix` | string | `v` | `release 1.2.3` and `release v1.2.3` both mean the same tag |
| `message` | string | `{project} {version}` | subject of the squash commit and the annotated tag. Placeholders: `{project}`, `{version}`, `{tag}` |
| `changelog` | string | `CHANGELOG.md` | the file whose section for this version must exist. `""` turns the check off |
| `version_files` | list | `[]` | files that must declare the version being cut — `version = "1.2.3"`, `"version": "1.2.3"` and `__version__ = "1.2.3"` all count |
| `wait` | bool | `true` | wait for the Forgejo build before exporting |
| `timeout` | int | `3600` | how long to wait for it |
| `poll` | int | `20` | seconds between polls |
| `workflow` | string | unset | wait on this one workflow (`build.yml`) instead of every one the tag started |

| command | what it does |
| --- | --- |
| `release 1.2.3` | the whole cycle |
| `release 1.2.3 --check` | print the plan and stop; changes nothing, anywhere |
| `release 1.2.3 --retag` | re-cut a version whose build failed — see below |
| `release 1.2.3 --from export` | resume from a step (`merge`, `tag`, `push`, `wait`, `export`) |
| `release 1.2.3 --no-wait` | push and stop; run `publish --tag` yourself once the build is green |
| `release 1.2.3 --no-export` | stop after the build; leave the mirror alone |

### The release commit is a tree copy, not a merge

What lands on the publication branch is the work branch's tree, whole, as one
commit whose parent is that branch's previous release. It is built with
`git commit-tree` rather than `git merge --squash`, which matters twice.

A publication branch is often **unrelated** to the work branch — butler's own
`main` is an orphan, because the published history was started fresh rather
than carrying the private one — and `git merge --squash` refuses that outright
("refusing to merge unrelated histories"). A tree copy does not care.

And working in plumbing means nothing is ever checked out. The publication
branch is written by `update-ref`, so your working tree is not switched, not
reset and not restored afterwards: an ignored build directory that costs an
hour to rebuild is still there when the release is out. The one thing that
cannot work is cutting a release *from* the publication branch — moving the
branch under its own index — and that is refused in the preflight.

### Everything is checked before anything moves

Nothing in the preflight writes, so a run that stops in it has changed nothing
on the machine and nothing on either forge:

- the working tree is clean, and `source` is pushed — the build and the export
  both read Forgejo, so work left behind here would be missing from the release
  without anything failing;
- the CHANGELOG has a section for this version. CI reads the same file to write
  the release notes, and discovering it empty there has already cost a tag, a
  push and a build;
- every file in `version_files` declares this version;
- `into` is where origin has it — that branch is written by releases only, so
  a local commit on it is a mistake worth stopping for;
- the tag does not already exist.

Then the plan is printed and confirmed (`--yes` for an unattended run).

### A failed build can be re-cut

A build that failed published nothing, so the version number is still free:

```
butler.py release 1.2.3            # …the build fails
git commit -am 'fix the build'     # on dev
git push origin dev
butler.py release 1.2.3 --retag
```

The squash commit is rebuilt from the fixed work branch, the tag moves onto it,
and both are force-pushed — the branch is force-pushed *with lease*, so a commit
someone else put on it since the fetch aborts the push rather than disappearing.
The publication branch still gains exactly one commit for the release, which is
what keeps a generated public history readable.

It stops being safe the moment anything has been published under that tag, and
`--retag` refuses there: a tag whose Forgejo release exists, or whose commit is
already on the mirror, is spent — moving it would leave a published release
describing a commit nobody can check out, and whoever downloaded it holding
artifacts built from code that is no longer anywhere. Cut the next version
instead.

The rewind is equally narrow: the only commit it will discard is the release
commit butler itself made. A publication branch sitting on anything else stops
the run rather than losing whatever is there.
