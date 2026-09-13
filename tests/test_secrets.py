"""Reading a token out of the machine's secrets file.

The file is parsed, never sourced — it lives on a developer's machine and a
`$(...)` in it must be data, not a command — and values never reach the
terminal, so the tests assert on which NAME was chosen rather than on what it
held.
"""

from pathlib import Path

from butler import secrets


def write(tmp_path, text):
    f = tmp_path / "secrets"
    f.write_text(text)
    return f


def test_reads_plain_assignments(tmp_path):
    f = write(tmp_path, "A=one\nB=two\n")
    assert secrets.read(f) == {"A": "one", "B": "two"}


def test_export_prefix_and_quotes_are_tolerated(tmp_path):
    f = write(tmp_path, "export A=\"one\"\nB='two'\n")
    assert secrets.read(f) == {"A": "one", "B": "two"}


def test_last_assignment_wins_and_a_missing_newline_still_counts(tmp_path):
    f = write(tmp_path, "A=one\nA=two")
    assert secrets.read(f) == {"A": "two"}


def test_the_file_is_parsed_not_sourced(tmp_path):
    # Sourcing this would run the command; parsing it yields the text.
    f = write(tmp_path, "A=$(rm -rf /)\n")
    assert secrets.read(f) == {"A": "$(rm -rf /)"}


def test_comments_and_blanks_are_ignored(tmp_path):
    f = write(tmp_path, "# a note\n\nA=one\n")
    assert secrets.read(f) == {"A": "one"}


def test_a_missing_file_is_no_secrets_not_an_error(tmp_path):
    assert secrets.read(tmp_path / "nope") == {}


def test_an_explicit_export_beats_the_file(tmp_path, monkeypatch):
    f = write(tmp_path, "TOK=from-file\n")
    monkeypatch.setenv("TOK", "from-env")
    assert secrets.lookup(["TOK"], f) == ("TOK", "from-env")


def test_the_environment_is_consulted_in_full_before_the_file(tmp_path, monkeypatch):
    # A one-off `FORGEJO_TOKEN=... butler publish` must win over the file's
    # read-only default, even though that name is tried first.
    f = write(tmp_path, "FIRST=from-file\n")
    monkeypatch.delenv("FIRST", raising=False)
    monkeypatch.setenv("SECOND", "from-env")
    assert secrets.lookup(["FIRST", "SECOND"], f) == ("SECOND", "from-env")


def test_names_are_tried_in_order(tmp_path, monkeypatch):
    for name in ("FIRST", "SECOND"):
        monkeypatch.delenv(name, raising=False)
    f = write(tmp_path, "FIRST=a\nSECOND=b\n")
    assert secrets.lookup(["SECOND", "FIRST"], f) == ("SECOND", "b")


def test_nothing_found_is_none_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    assert secrets.lookup(["NOPE"], tmp_path / "missing") is None


def test_a_token_is_named_for_the_host_it_belongs_to():
    # A credential is only ever sent to the service it was issued for, and the
    # name is what says which that is.
    assert secrets.host_suffix("git.example.org") == "GIT_EXAMPLE_ORG"
    assert secrets.host_suffix("git-vaelum.dev") == "GIT_VAELUM_DEV"
    assert "FORGEJO_TOKEN_FOR_GIT_EXAMPLE_ORG" in secrets.forgejo_names("git.example.org")


def test_the_read_only_token_is_preferred():
    # Everything the harness does with this token reads: it fetches a release
    # somebody else's CI built.
    names = secrets.forgejo_names("h")
    assert names[0] == "FORGEJO_TOKEN_READONLY"
    assert names.index("FORGEJO_TOKEN_READONLY") < names.index("FORGEJO_TOKEN")


def test_the_path_is_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETS_FILE", str(tmp_path / "elsewhere"))
    assert secrets.path() == tmp_path / "elsewhere"
    monkeypatch.delenv("SECRETS_FILE")
    assert secrets.path() == Path.home() / ".secrets"
