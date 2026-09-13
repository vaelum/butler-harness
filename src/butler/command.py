"""The command tree.

One node type for components, actions and sub-actions alike. `app`, `android`
and `build` are the same kind of thing at different depths, so modelling them
separately (as the hand-rolled butlers did — a component subparser, then a
positional `choices=[...]` for Android) buys nothing and costs the nested level
its own --help and its own flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

Handler = Callable[..., int | None]


@dataclass(frozen=True)
class Arg:
    """One argparse argument, declared where the action is declared."""
    flags: tuple[str, ...]
    kwargs: dict[str, Any]


def arg(*flags: str, **kwargs: Any) -> Arg:
    return Arg(flags, kwargs)


@dataclass
class Node:
    name: str
    help: str = ""
    args: list[Arg] = field(default_factory=list)
    # Leaf nodes have a handler; branch nodes have children. A node with both is
    # a branch whose handler runs when invoked bare (used by `dev`).
    func: Handler | None = None
    children: list["Node"] = field(default_factory=list)
    epilog: str | None = None

    def child(self, name: str) -> "Node | None":
        return next((c for c in self.children if c.name == name), None)

    def add(self, node: "Node", *, replace: bool = True) -> "Node":
        existing = self.child(node.name)
        if existing is not None:
            if not replace:
                return existing
            self.children[self.children.index(existing)] = node
        else:
            self.children.append(node)
        return node


def find(roots: list[Node], path: list[str]) -> Node | None:
    """Resolve a dotted task path ('server.claim-token') against the tree."""
    nodes, node = roots, None
    for part in path:
        node = next((n for n in nodes if n.name == part), None)
        if node is None:
            return None
        nodes = node.children
    return node


def ensure(roots: list[Node], path: list[str]) -> Node:
    """Resolve a path, creating bare branch nodes for any missing ancestors.

    This is what lets butler_tasks.py declare `@task("node.run")` in a project
    that has no `node` component at all — the branch springs into existence.
    """
    nodes = roots
    node: Node | None = None
    for part in path:
        found = next((n for n in nodes if n.name == part), None)
        if found is None:
            found = Node(name=part)
            nodes.append(found)
        node = found
        nodes = found.children
    assert node is not None
    return node
