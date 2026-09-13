"""Deploying a server to a box over SSH.

The shape is the same in every project that does it: stop the stack so the data
is quiescent, snapshot it, rsync the code in without touching the live data
directory or the production .env, then rebuild and start.

Two things the copies disagreed on, resolved here:

  * the snapshot — one copied just the sqlite file, another zipped the whole
    data directory. The zip is a genuine restore point (WAL and sidecars
    included, plus whatever else the app keeps beside the DB): stop the stack,
    replace data/ with the zip's contents, start again, and the instance is
    exactly what it was. It's the default; `backup = "sqlite-file"` keeps the
    cheaper behaviour for projects that only have a DB.
  * first deploy — one of them probes for it and polls the fresh instance's logs
    for the one-time claim token. Without that the very first deploy leaves you
    with a running server you can't log into without going and reading container
    logs by hand.

`build_first = true` reverses the middle of that sequence: rsync and build while
the OLD stack is still serving, and only then stop, snapshot and start. Where
the image build is slow (a Node toolchain, a Playwright browser download) this
is the difference between a swap measured in seconds and an outage measured in
minutes. It is only correct when the image COPYs its source in — see the config
comment on the flag.
"""

from __future__ import annotations

import os
import shlex
from datetime import datetime

from .. import proc, ui
from ..config import DeployConfig, ServerConfig
from ..context import Ctx
from ..errors import ButlerError

COMPOSE_NAMES = ("docker-compose.yml", "compose.yml",
                 "docker-compose.yaml", "compose.yaml")


def ssh_sh(ctx: Ctx, host: str, script: str) -> int:
    """Run a POSIX shell script on `host`. See proc.feed for why via stdin."""
    return proc.feed(["ssh", "-T", host, "sh", "-s"], script.strip() + "\n",
                     dry_run=ctx.dry_run)


def _require(ctx: Ctx, host: str, script: str, what: str) -> None:
    rc = ssh_sh(ctx, host, script)
    if rc != 0:
        raise ButlerError(f"deploy aborted: {what} failed", code=rc)


def compose_cmd(cfg: DeployConfig) -> str:
    return "docker compose" + (f" -f {cfg.compose_file}" if cfg.compose_file else "")


def compose_present(cfg: DeployConfig) -> str:
    """A shell test for "is there a stack to act on here?".

    Needed because the stack is stopped BEFORE the first rsync, when the deploy
    directory is still empty.
    """
    names = [cfg.compose_file] if cfg.compose_file else COMPOSE_NAMES
    return " || ".join(f"[ -f {cfg.dir}/{name} ]" for name in names)


def data_path(cfg: DeployConfig) -> str:
    """Where the data directory is on the host.

    Usually inside the deploy directory; an absolute or ~-rooted `data_dir` is a
    bind mount that deliberately lives outside it, so that redeploying — or
    deleting the deploy directory outright — cannot touch the data.
    """
    return cfg.data_dir if cfg.data_dir.startswith(("/", "~")) \
        else f"{cfg.dir}/{cfg.data_dir}"


def is_first_deploy(ctx: Ctx, cfg: DeployConfig) -> bool:
    """No data directory on the far end means nothing to preserve.

    `test` exits 1 for "absent"; anything above that is ssh itself failing, and
    must not be mistaken for a clean first deploy — that would skip the backup.
    """
    rc = ssh_sh(ctx, cfg.ssh_host,
                f"mkdir -p {cfg.dir}\ntest -d {data_path(cfg)}")
    if rc not in (0, 1):
        raise ButlerError("deploy aborted: could not reach the deploy host", code=rc)
    return rc == 1


def ensure_data_dir(ctx: Ctx, cfg: DeployConfig) -> None:
    """Create the data directory before anything mounts it.

    A bind mount whose source is missing is created by the daemon as root, and
    a service running as anyone else then cannot write to its own data.
    """
    _require(ctx, cfg.ssh_host, f"mkdir -p {data_path(cfg)}",
             "creating the data directory")


def preflight_script(cfg: DeployConfig, first: bool) -> str:
    """Check the backup tool is there. Whatever runs this must run it BEFORE the
    stack goes down — discovering that `zip` isn't installed after taking the
    service offline is a bad trade."""
    if first or cfg.backup != "zip-data-dir":
        return ""
    return ("command -v zip >/dev/null 2>&1 || "
            "{ echo 'zip is not installed on the deploy host'; exit 1; }\n")


def stop_stack(ctx: Ctx, cfg: DeployConfig, *, preflight: str = "") -> None:
    """Stop whatever is running, if anything is."""
    _require(ctx, cfg.ssh_host, preflight + f"""
if {compose_present(cfg)}; then
  cd {cfg.dir} && {compose_cmd(cfg)} down
else
  echo 'no compose file in {cfg.dir} — nothing to stop'
fi""", "stopping the existing stack")


def snapshot(ctx: Ctx, cfg: DeployConfig, stamp: str) -> str | None:
    """Take the pre-deploy restore point. Returns where it landed."""
    if cfg.backup == "none":
        return None
    assert cfg.backup_dir

    if cfg.backup == "zip-data-dir":
        target = f"{cfg.backup_dir}/{ctx.name}-{stamp}.zip"
        # Zipped from the data directory's PARENT, so the paths inside are
        # `data/...` and it unpacks straight back over the original.
        _require(ctx, cfg.ssh_host, f"""
set -e
mkdir -p {cfg.backup_dir}
data={data_path(cfg)}
cd "$(dirname "$data")" && zip -rq {target} "$(basename "$data")"
echo "backed up -> {target} ($(du -h {target} | cut -f1))" """, "the data-dir backup")
        return target

    target = f"{cfg.backup_dir}/{ctx.name}-{stamp}.db"
    # Copy the WAL/SHM sidecars too, so the snapshot is internally consistent.
    _require(ctx, cfg.ssh_host, f"""
set -e
mkdir -p {cfg.backup_dir}
cp -a {data_path(cfg)}/{cfg.db_file} {target}
for ext in -wal -shm; do
  if [ -f {data_path(cfg)}/{cfg.db_file}$ext ]; then
    cp -a {data_path(cfg)}/{cfg.db_file}$ext {target}$ext
  fi
done
echo 'backed up -> {target}'""", "the database backup")
    return target


def push_code(ctx: Ctx, server: ServerConfig, cfg: DeployConfig) -> None:
    """rsync the server directory in. The trailing slash on the source copies
    its CONTENTS, and --delete then prunes files removed from the repo — hence
    the excludes: live data, the production .env and build junk must survive."""
    excludes = [f"--exclude={e}" for e in cfg.exclude]
    if cfg.host_file:
        # Local-only, and the address of the host is not the host's business.
        excludes.append(f"--exclude={cfg.host_file.name}")
    rc = ctx.run(["rsync", "-az", "--delete", *excludes,
                  f"{server.dir}/", f"{cfg.ssh_host}:{cfg.dir}/"])
    if rc != 0:
        raise ButlerError("deploy aborted: rsync failed", code=rc)


def build_stack(ctx: Ctx, cfg: DeployConfig) -> None:
    """Build the image on the host. With `build_first` the old stack is still
    serving while this runs, which is the entire point of the flag."""
    env = ""
    for name in cfg.build_env:
        value = os.environ.get(name)
        if value is not None:
            env += f"{name}={shlex.quote(value)} "
    rc = ssh_sh(ctx, cfg.ssh_host, f"cd {cfg.dir} && {env}{compose_cmd(cfg)} build")
    if rc != 0:
        raise ButlerError("deploy aborted: the image build failed", code=rc)


def start_stack(ctx: Ctx, cfg: DeployConfig, *, build: bool = True) -> None:
    build_flag = " --build" if build else ""
    rc = ssh_sh(ctx, cfg.ssh_host,
                f"cd {cfg.dir} && {compose_cmd(cfg)} up -d{build_flag}")
    if rc != 0:
        raise ButlerError("deploy failed: docker compose up failed", code=rc)


def watch_for(ctx: Ctx, cfg: DeployConfig, service: str, prefix: str) -> None:
    """Poll a fresh instance's logs for a one-time value it prints on boot."""
    rc = ssh_sh(ctx, cfg.ssh_host, f"""
cd {cfg.dir}
tries=0
while [ $tries -lt 15 ]; do
  value=$({compose_cmd(cfg)} logs {service} 2>/dev/null | sed -n 's/.*{prefix} *//p' | tail -n 1)
  [ -n "$value" ] && {{ echo "{prefix} $value"; exit 0; }}
  tries=$((tries+1)); sleep 2
done
echo 'nothing matched in the logs yet'; exit 1""")
    if rc != 0:
        ui.warn("could not read it from the logs", "— check the container output:\n"
                f"  ssh {cfg.ssh_host} 'cd {cfg.dir} && {compose_cmd(cfg)} logs {service}'")


def deploy(ctx: Ctx, server: ServerConfig) -> int:
    cfg = server.deploy
    if cfg is None:
        raise ButlerError("this project has no [server.deploy] configuration")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    first = is_first_deploy(ctx, cfg)
    if first:
        ui.warn("first deploy", f"— no {data_path(cfg)} yet, nothing to back up")
    preflight = preflight_script(cfg, first)

    if cfg.build_first:
        # The image COPYs the source in, so neither the rsync nor the build
        # disturbs the container that is still serving. Downtime is the three
        # steps after them, not the build.
        push_code(ctx, server, cfg)
        ensure_data_dir(ctx, cfg)
        if preflight:
            _require(ctx, cfg.ssh_host, preflight, "the pre-deploy check")
        build_stack(ctx, cfg)
        stop_stack(ctx, cfg)
        backup = None if first else snapshot(ctx, cfg, stamp)
        start_stack(ctx, cfg, build=False)
    else:
        stop_stack(ctx, cfg, preflight=preflight)
        backup = None if first else snapshot(ctx, cfg, stamp)
        push_code(ctx, server, cfg)
        ensure_data_dir(ctx, cfg)
        start_stack(ctx, cfg)

    ui.plain()
    ui.ok("deployed", f"{cfg.ssh_host}:{cfg.dir}" + (f"  (backup: {backup})" if backup else ""))
    if first and cfg.post_deploy_watch:
        watch_for(ctx, cfg, server.service, cfg.post_deploy_watch)
    return 0
