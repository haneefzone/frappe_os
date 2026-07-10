"""Production-setup config capture + diff (session 2.5).

`bench setup production` rewrites the server's nginx and supervisor config to
serve the bench's sites under supervisor/nginx instead of the dev `bench start`.
Before we run it we capture a hashed manifest of `/etc/nginx` and
`/etc/supervisor` (and a full tar pre-backup for rollback); after it runs we
capture the manifest again and diff the two so the job timeline shows exactly
which config files the conversion added, removed or changed.

These are pure functions over the `fdm-elevate manifest` output (one
`sha256␠␠path` line per file, `sha256sum` format), so they unit-test without any
SSH/root. The elevated capture itself lives in the `deploy/fdm-elevate` helper;
the action orchestrates it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The config trees `bench setup production` touches, and the only paths the
# fdm-elevate helper reads/tars. Kept here so the action and the docs agree.
CAPTURED_CONFIG_ROOTS = ("/etc/nginx", "/etc/supervisor")


def parse_manifest(stdout: str) -> dict[str, str]:
    """Parse `sha256sum`-style lines (``<hex>  <path>``) into ``{path: sha256}``.

    Ignores blank lines and any non-manifest chatter (a line without at least a
    hash and a path). The helper prints one line per file under the captured
    roots; a mode/permission error on a single file is skipped by the helper, so
    a missing line simply means that file wasn't readable, not that it changed.
    """
    manifest: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        # sha256sum emits "<64-hex><space><space><path>"; be lenient on spacing.
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        sha, path = parts[0], parts[1].strip()
        # A sha256 is 64 hex chars; skip anything that isn't a plausible hash so
        # stray log lines never masquerade as a config file.
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha.lower()):
            continue
        if path:
            manifest[path] = sha.lower()
    return manifest


@dataclass
class ConfigDiff:
    """The before→after delta of the captured config trees."""

    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unchanged: int = 0

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.removed) + len(self.changed)

    def as_dict(self) -> dict[str, object]:
        return {
            "added": self.added,
            "removed": self.removed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "total_changes": self.total_changes,
        }


def diff_manifests(before: dict[str, str], after: dict[str, str]) -> ConfigDiff:
    """Diff two path→sha256 manifests into added / removed / changed / unchanged.

    Deterministic (sorted) so the emitted timeline and the JSON result line are
    stable across runs.
    """
    before_paths = set(before)
    after_paths = set(after)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    changed = sorted(p for p in (before_paths & after_paths) if before[p] != after[p])
    unchanged = sum(1 for p in (before_paths & after_paths) if before[p] == after[p])
    return ConfigDiff(added=added, removed=removed, changed=changed, unchanged=unchanged)
