"""Component kinds: each turns a slice of butler.toml into a command subtree."""

from __future__ import annotations

from ..command import Node
from ..config import Config


def tree(cfg: Config) -> list[Node]:
    """The command tree implied by a project's configuration."""
    from . import cmake, extension, lint
    from . import publish as publish_component
    from . import server as server_component
    from . import tauri

    nodes: list[Node] = []
    if cfg.app:
        nodes.append(tauri.node(cfg.app))
    if cfg.server:
        nodes.append(server_component.node(cfg.server))
    if cfg.extension:
        nodes.append(extension.node(cfg.extension))
    if cfg.build:
        nodes.append(cmake.node(cfg.build))
    if cfg.check:
        nodes.append(lint.node(cfg.check, cfg.build))
    if cfg.publish:
        nodes.append(publish_component.node(cfg.publish))
    return nodes
