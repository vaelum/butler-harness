"""GUIDE.md's reference section, generated from types.py.

`python -m butler.planning.guide` rewrites it in place; a test fails when the
file and the types disagree, so the guide can never describe another format.
"""

from __future__ import annotations

from pathlib import Path

from .types import reference

PATH = Path(__file__).resolve().parent / "GUIDE.md"
START, END = "<!-- reference:start -->", "<!-- reference:end -->"


def rendered(text: str) -> str:
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n{reference()}{END}{tail}"


def main() -> None:
    text = PATH.read_text()
    new = rendered(text)
    if new != text:
        PATH.write_text(new)
        print(f"updated {PATH}")
    else:
        print(f"{PATH} is current")


if __name__ == "__main__":
    main()
