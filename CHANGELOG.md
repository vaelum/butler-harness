# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

A project pins the harness by tag in its `butler.py` (`HARNESS_REF`), so a tag
here is what projects actually consume — keep these sections accurate before
tagging. `butler.py --version` reports the version in use and the pin the
project asked for.

## [0.8.1]

### Added

- **The harness runs on itself.** This repository now has the `butler.py` every
  other project has — except that it is not a bootstrap shim. It runs `src/`
  directly, because a harness that pinned a release of itself could not ship
  the fix to its own release step: the code that cuts the release is the code
  being released, which is the only arrangement in which `release --retag`
  means anything here.

  So the harness's whole life is one entry point: `butler.py test`,
  `butler.py build`, `butler.py publish check`, `butler.py release 0.8.1`.
  `test` and `build` are two tasks in `butler/butler_tasks.py` — pytest against
  a src/ layout and a wheel are true of this repository, not of the projects it
  serves, which is exactly what butler_tasks.py is for.

- **A `v*` tag now publishes a Forgejo release** with the wheel and the sdist
  attached and this version's CHANGELOG section as the notes
  (`.forgejo/workflows/ci.yml`), and `[publish] release = true` copies it onto
  the GitHub mirror. Until now a release of the harness was a tag and nothing
  else — `release` waited for a build, exported the tag, and had no release to
  copy. The job refuses to publish a wheel whose version is not the one the tag
  names, and refuses a version with no CHANGELOG entry, so neither can reach a
  project that pins it.

### Changed

- **`release` pushes the tag first and the publication branch last.** The steps
  are now `merge`, `tag`, `push`, `wait`, `land`, `export`: `push` sends only
  the tag, which is what starts the build, and `land` moves the branch once
  that build has passed.

  Cutting 0.8.1 is what found it. The first ordering pushed branch and tag
  together, its build failed, and the re-cut bounced off `branch main is
  protected from force push` — with the failed release already sitting on the
  public branch. Now a failed build has pushed nothing but a tag: re-cutting
  rewinds nothing that was ever published, the fixed commit lands as a plain
  fast-forward, and the publication branch can stay force-push protected, as a
  branch written by releases only should be.

  A run told `--no-wait` stops after the tag; `release <version> --from land`
  finishes it when the build goes green, and is the recovery for a run
  interrupted anywhere after the tag.

- **`release` asks whether the forge can publish a release at all**, before it
  tags anything. A Forgejo repository can have its Releases unit switched off,
  and then every releases endpoint answers `404` — indistinguishable from a
  wrong URL, a missing release, or a token without the scope. The butler
  repository had it off, so the new release job built its artifacts, wrote its
  notes and died on `POST /releases` with nothing to say why; it cost three
  builds and two wrong diagnoses (the workflow's `permissions:` block, which
  turned out to be innocent) before anyone looked at the repository settings.
  Now it is one refusal, before the tag, naming the checkbox.

- The CI workflow declares no `permissions:` block, matching every other
  repository here that publishes releases.

- `build>=1` is in the `dev` extra, so a checkout set up the documented way can
  build the same wheel CI does without installing anything globally.

## [0.8.0]

### Added

- **`release` — the whole cycle, in one command.** A project with a `[release]`
  section gets `butler.py release 1.2.3`, which does what every release here
  was doing by hand: put the work branch on the publication branch as one
  commit, tag it, push to Forgejo, wait for the build that publishes the
  release, then export the tag to the mirror and copy that release onto it.

  That commit is a tree copy (`git commit-tree`), not a `git merge --squash`.
  A publication branch is often unrelated to the work branch — butler's own
  `main` is an orphan — and merge refuses that outright. It also means nothing
  is checked out: the branch is written with `update-ref`, so the working tree
  the release was cut from is untouched, and there is nothing to restore if a
  run stops halfway.

  The steps were never the hard part; the order is. Exporting before the build
  finishes puts a public tag on the mirror whose release never arrives, and
  tagging a version whose CHANGELOG entry was forgotten produces a release
  nobody can read — which is how chords came to ship `v2026.9.2` twice. So the
  notes, the version strings, the pushed state of the work branch and the state
  of the tag are all checked **before** anything moves, and `--check` prints the
  whole plan without touching a thing.

- **`release --retag`: re-cut a version whose build failed.** A failed build has
  published nothing, so the version number is still free. Fix it, commit on the
  work branch, and re-cut the same version: the squash commit is rebuilt from
  the fixed branch, the tag moves onto it, and both are force-pushed. The
  publication branch still gains exactly **one** commit for the release, which
  is the property that makes a generated public history readable at all.

  It refuses the moment that stops being safe. A tag whose Forgejo release
  exists, or whose commit is already on the mirror, is spent: moving it would
  leave a published release describing a commit nobody can check out. The
  branch is only ever rewound past the release commit butler itself made — one
  sitting on anything else stops the run.

- `wait_for_build` in `release.py`, which polls the Forgejo Actions API for the
  runs a tag started. It matches on the ref *and* the commit (a moved tag has
  old runs under the same name), takes the newest run of each workflow (so a
  re-run replaces the failure it re-ran), and says the state only when it
  changes.

## [0.7.1]

### Fixed

- **A `--docker` build's JUnit report went to a path nothing reads**, so every
  CI job that uploads test results uploaded none. The buildenv script handed
  `ctest` a RELATIVE `--output-junit`, and `ctest --preset` runs in the
  preset's binary directory rather than the shell's working directory — so a
  report asked for at `builds/release/test-results.xml` was written to
  `builds/release/builds/release/test-results.xml`.

  It failed silently in both directions: ctest creates the missing directories
  and exits 0, and the upload step on the other side is usually configured
  `if-no-files-found: warn`, so a green job uploaded nothing and said so only
  in a log line. In yeet it had been doing that for 55 consecutive runs, and
  the sanitizer nightlies had never once produced the results they exist to
  produce.

  The path is now absolute inside the container (`<workdir>/<build_dir>/…`),
  which is what the HOST path always passed — `_build_on_host` uses
  `cfg.preset_dir(...)` and so never had the bug. Projects consuming this get
  their test results back with no change on their side beyond the pin.

## [0.7.0]

### Added

- `publish --tag` copies the tag's **release** to the mirror when the project
  sets `release = true`: assets, notes, title and the pre-release flag. The
  installers are built where the code is private — a `v*` tag runs the
  project's Forgejo CI — and the mirror has no CI of its own, since a workflow
  file is one of the things the export holds back. So the public release is not
  built again, it is copied.

  This was the last thing `push-public.sh` did that the component could not,
  and the reason seven projects still carried a ~300-line script next to a
  ten-line `[publish]` block.

  The order is the design: the release is fetched and checked *before* the
  export pushes anything. A build still running, a draft, an empty release, or
  a tag on Forgejo naming another commit than the local one each stops the run
  with nothing published — rather than leaving a public tag whose release never
  arrives. A run that failed partway is simply repeated: a tag already on the
  right commit is left alone, and an existing GitHub release gets only the
  assets it is missing.

- `butler.secrets`: reading an API token from the environment or the machine's
  `~/.secrets` (`SECRETS_FILE` overrides the path). The file is parsed, never
  sourced — it is a list of `KEY=value` lines, and sourcing would execute
  whatever else ended up in it — and values never reach the terminal: a run
  says which NAME it used, so a wrong token surfaces as the service's own 401.

  For Forgejo the read-only name is tried first, then one named for the host
  (`FORGEJO_TOKEN_FOR_GIT_EXAMPLE_ORG`), then `FORGEJO_TOKEN`; an explicit
  export always beats the file. Copying a release only ever *reads* from
  Forgejo, so it has no business holding a writable token when a narrower one
  exists.

### Fixed

- An asset download that Forgejo redirects to its object storage no longer
  carries the Forgejo token to that other host. urllib re-sends every header
  across a redirect; `curl`, which the shell script used, drops it.

## [0.6.6]

### Fixed

- The export strips the `[publish]` table from `butler/butler.toml`. The table
  is export machinery, like `copy.bara.sky` and `push-public.sh`, and it names
  the private Forgejo host the mirror is generated from — which is exactly what
  making `host` required was meant to keep out of published repositories. The
  rest of the file says how the project is built and still ships.

  Three faults in that one transformation showed up only when a real export
  loaded it: Starlark rejects an unescaped bracket in a plain string, Copybara
  matches with RE2 and so has no lookahead, and a transformation matching no
  file is an error rather than a no-op on history that predates the table.

## [0.6.5]

### Added

- A `publish` component: `butler publish` exports a branch of the private
  Forgejo repository to a generated GitHub mirror through Copybara, driven by a
  `[publish]` section naming the two repositories, the branch and the author.
  Everything else — the pinned Copybara jar and its hash, the JVM lookup, the
  "is the branch pushed" guard, the identity pinning, the `PRIVATE:` commit
  scrubber, tag resolution through `GitOrigin-RevId`, and the baseline exclude
  list — lives in the harness instead of in each project.

  This replaces a hand-written `copy.bara.sky` plus a ~200-line
  `push-public.sh` per project with about ten lines of TOML. It also removes
  the failure mode those copies shared: each stated its exclude list **twice**,
  as a Copybara glob and again as a shell regex, under a comment asking that
  the two be kept in sync. The globs are now the only source and the regex is
  derived from them.

- `[publish] host` is required rather than defaulted. A default would bake one
  Forgejo instance into a harness that is itself published, and put that
  hostname in the docs and tests of every copy of it.

- `butler publish check` lists what a project would and would not publish,
  without touching the network — and says so when a public export is holding
  nothing back.

- `butler publish --rehearse` runs the export into a scratch bare repository
  and prints the tree, so an exclude list can be verified before anything
  becomes a permanent public commit. It earned its keep immediately: it caught
  a config that would not parse, a transform Copybara refuses to load without
  an explicit reversal, and a Markdown glob that silently rewrote nothing.

### Changed

- The harness is published at
  [github.com/vaelum/butler-harness](https://github.com/vaelum/butler-harness),
  and `butler new` scaffolds shims pinned there rather than at Forgejo. A shim
  pinned at a host the reader cannot reach makes a published project
  unbuildable by anyone but its author. Projects on an older pin keep working
  and are repointed as they are migrated.

- The export repoints `butler.py`'s harness pin at the public harness on the
  way out (`rewrite_harness`, on by default). A shim pinned at Forgejo makes a
  published repository unbuildable by anyone who cannot reach it, which is
  every reader of a public mirror.

## [0.6.4]

The harness moved from GitHub to the private Forgejo instance, and so did the
projects that vendor dependencies through it.

### Changed

- `butler new` scaffolds shims that fetch the harness from
  `git+https://github.com/vaelum/butler-harness.git`, pinned to this
  release. The GitHub URL is gone: that repository is archived, and the name
  now belongs to a private export that anonymous installs cannot reach.

### Fixed

- A `git_deps` checkout whose `origin` disagrees with the `url` in
  `butler.toml` is repointed before it is fetched. The toml URL was only ever
  used for a fresh clone; an existing checkout fetched whatever it was cloned
  with, so moving a dependency to another host left every existing tree
  fetching the old one — failing to find the new `ref`, warning, and building
  the stale checkout it already had. Now the first build after the move heals
  it, and says so.

## [0.6.3]

Found while migrating `lifestack`, the last of the four.

### Security

- The Android signing passwords are passed to `apksigner` through the
  environment (`--ks-pass env:`) instead of `pass:` on the command line. butler
  echoes every command it runs, so the keystore password was printed to the
  terminal and into any CI log holding a build, and was readable from `/proc`
  while apksigner ran. All three hand-rolled scripts did this; it moved here
  unexamined in 0.3.0.

### Fixed

- `app icon` no longer fails under `--dry-run` when `icon_generator` is set. The
  generator is what writes `app-icon.png`, and the flag stops it running — so
  demanding its output reported a problem that only the flag created.

## [0.6.2]

### Fixed

- The version is single-sourced from `butler.__version__`, with `pyproject.toml`
  deriving it. They were two hand-edited copies, and 0.6.1 shipped reporting
  itself as 0.6.0 through `butler.py --version`.

## [0.6.1]

### Changed

- `build --help` describes `--docker` in terms of what it can actually change.
  A project whose only buildenv presets are also in `always` — yeet, where
  `cachyos` is the sole image — was told the flag was "available for: cachyos"
  while every invocation of it was either redundant or an error; it now says so
  outright. Where an optional buildenv does exist, the help names those presets
  instead, and the host-vs-container build directory warning is emitted only
  then, since that clash cannot arise for an always-container preset.

## [0.6.0]

What migrating `yeet` — the largest and most bespoke of the hand-rolled scripts —
asked of the components built in 0.5.0.

### Added

- Per-preset buildenv images: `dockerfile`, `tag` and `context` may contain
  `{preset}`, `presets` says which presets have one, and `always` marks a preset
  that builds in its container with or without `--docker` — for a target whose
  image *is* its toolchain, where a host build was never a meaningful request.
- `[build] prepare` — a per-preset script run before configuring, if it exists.
- `[build.test_env]` `up`/`down` accept several commands, not just one: standing
  services up is usually start-the-stack plus a readiness barrier.
- `[build.test_env] requires` — paths that must exist before a test run, checked
  up front. A compose file a dependency supplies, or `/dev/fuse`, both otherwise
  fail deep and unrecognisably.
- `[build.test_env] docker_args`, replacing `network`: everything a containerised
  test run needs and a build does not, including a device and capabilities.
- `[check.lint]` `clear`/`keep` — empty the results directory first, preserving
  another stage's artifact. MegaLinter reuses it in place and the project-scope
  scanners read it back, which is how a fixed finding keeps resurfacing. A
  non-default `reports` is now also passed as `REPORT_OUTPUT_FOLDER`.
- `[check.tidy]` `docker`/`args` — run the driver inside the buildenv, because a
  compile database produced in a container references headers only found there.
- `[check] report` — a command run after the stages, always, including when a
  stage found things. Also available on its own as `check report`.
- Submodules and `git_deps` are now refreshed by Tauri and Android builds too,
  not only CMake ones: a Rust build script that CMake-builds a C++ tree needs
  those checkouts just as much, and failed deep inside CMake without them.

### Fixed

- `--dry-run` no longer fetches. Refreshing submodules and `git_deps` ran for
  real under the flag, which is a network mutation of the working tree.
- A `--docker` build without `--test` no longer starts the test services, or
  passes the container flags that exist for the tests' sake.

### Changed

- A branch that only exists because a `butler_tasks.py` task created it is
  labelled in `--help` instead of sitting blank.

## [0.5.0]

The C++ components, built while migrating `vl` off its hand-rolled script.

### Added

- `[build]` — CMake presets on the host or inside a project-supplied buildenv
  container (`build <preset> [--test] [--test-filter RE] [--docker]`). One build
  directory per preset is enforced by the config loader, ctest writes JUnit where
  CI expects it, and `[project] submodules` / `git_deps` are refreshed first.
- `[[build.option]]` — a named build switch, exposed as one CLI flag and a set of
  CMake cache variables. The flag polarity follows the declared default, so it
  always reads as the thing it changes (`--no-storage`, `--lto`).
- `[build.test_env]` — services the test suite talks to, started around a test run
  and always torn down. `needs` ties them to a build option, so compiling the
  clients out also stops the services being started.
- `[check]` — MegaLinter over the tree and clang-tidy over the sources, together
  or one at a time. Bare `check` runs every configured checker and reports all of
  them rather than stopping at the first failure.
- `doctor` now covers cmake (including "found, but older than this project needs"),
  ninja, CMakePresets.json, the buildenv Dockerfile, and the check tooling.

### Notable behaviour

- A containerised test run uses `ctest --preset`, like the host path, rather than
  `cd`-ing into the build tree and invoking ctest bare. The two hand-rolled
  scripts disagreed here; the preset form keeps one definition of what the test
  run is.
- Build options are applied on every configure, not only the first, so a build
  directory previously configured the other way is corrected instead of silently
  reused.
- Unknown presets are rejected by the parser (they are `choices` on the
  positional), and a stale first word from an old CLI is mapped forward:
  `butler.py release --build` now answers "did you mean: butler.py build release".

## [0.4.0]

`[server.deploy]` grew the four things that were keeping chords on its own
`scripts/deploy.sh`. That script is gone; chords now deploys through the
built-in, with only the project-specific parts (the frontend precompile, the
OpenRouter key, the admin passcode) in its `butler_tasks.py`.

### Added

- `host_file` — the ssh target read from a gitignored file at deploy time,
  instead of `host` in the committed config. A public repo should not carry a
  server address. Resolved late, so `--help` and `doctor` still work in a clone
  that hasn't got one; a missing or empty file says how to create it. The file
  is excluded from the rsync automatically.
- `compose_file` — for stacks whose Compose file isn't at the root of the deploy
  directory (`docker/docker-compose.yml` and friends). Settable on `[server]`,
  where it also applies to `dev` / `down` / `logs` / `reset`, and inherited by
  `[server.deploy]`.
- `build_first` — build the new image while the OLD stack is still serving, then
  stop, snapshot and start: `rsync → build → stop → snapshot → start`. Downtime
  becomes the swap rather than the build, which for an image that pulls a Node
  toolchain and a Playwright browser is minutes. Only correct when the image
  COPYs its source in, and documented as such.
- `build_env` — forward named local environment variables to the remote build
  (`build_env = ["APT_MIRROR"]`), for build-time knobs. Only variables that are
  set are passed.

### Changed

- `data_dir` may be absolute or `~`-rooted, for data that lives outside the
  deploy directory as a bind mount. The probe, the zip (taken from the data
  directory's parent, so the archive still unpacks over the original) and the
  sqlite copy all follow it.
- Every deploy now `mkdir -p`s the data directory before the stack starts. A
  bind mount whose source is missing is created by the daemon as root, and the
  service then cannot write to its own data.

## [0.3.0]

First published release.

### The harness

- One CLI grammar for every project: `butler.py <component> <action>`, built
  from `butler/butler.toml`. A command exists only when the config declares that
  component, so `--help` always describes the project accurately.
- Components: `app` (Tauri desktop + Android), `server` (FastAPI, Compose or the
  project's own scripts), `extension` (Chrome + Firefox bundles from one source
  tree), and `doctor`, which reports the whole toolchain up front instead of
  letting a build discover a missing SDK ten minutes in.
- `butler/butler_tasks.py` escape hatch: `@task("a.b.c")` adds or replaces a
  command, `wraps=True` decorates a built-in. Handlers are introspected by
  parameter name (`ctx`, `args`, `inner`).
- Strict config loading — unknown keys and wrong types are errors naming the
  offending key, so a typo can never silently do nothing.
- `--dry-run`, `--verbose`, `--yes` and `--no-color` work at any depth, before
  or after the subcommand. Dry-run covers butler's own filesystem writes, not
  just the commands it shells out to.
- `--version` reports what is running, where from, and the project's pin.
- Stdlib only. The bootstrap shim is ~90 lines and pins an exact ref, so a
  project is never silently upgraded; `BUTLER_HARNESS_PATH` runs a working tree
  with no install at all.

### Notable behaviour

Where the hand-rolled scripts this replaces had drifted apart, the better
implementation won:

- Android cleartext uses a `network_security_config.xml` rather than the
  `usesCleartextTraffic` manifest placeholder — it takes precedence on API 24+
  and keeps the system trust anchors, so HTTPS is unaffected.
- `app/build/outputs` is always cleared before an Android build. Gradle leaves
  an APK behind for every variant ever built and the signing step globs the
  tree, so a stale APK would otherwise be re-signed with today's label and
  shipped as if it were the current build.
- Sideloading picks the split APK matching the device's `ro.product.cpu.abi`,
  falling back to a universal one; unsigned and aligned intermediates are never
  install candidates.
- The default deploy snapshot zips the whole data directory — a genuine restore
  point — with a single-sqlite-file copy (plus its WAL/SHM sidecars) as an
  option.
- Helpers raise `ButlerError` instead of calling `sys.exit()`, so a wrapping
  task can catch and carry on.
- The shim hands off through the venv's console script rather than
  `python -m butler`: the project root is on `sys.path` for `-m`, so `butler.py`
  would shadow the installed package and the shim would re-exec itself forever.
