"""
Normalize Jenkins SCM change data across Git, SVN, and Mercurial backends.

Key API difference:
  - Git:              build JSON has a "changeSets" key (plural, array of sets)
  - SVN / Perforce /
    Mercurial / other: build JSON has a "changeSet" key (singular, one object)

Each SCM backend also uses a different field name for the commit identifier:
  - Git:        "commitId"
  - SVN:        "revision"
  - Mercurial:  "commitId" (same as Git)
  - Generic:    "id" (fallback)
"""

from __future__ import annotations


def _commit_id(item: dict) -> str:
    """Extract a commit/revision identifier regardless of SCM backend."""
    for field in ("commitId", "revision", "id"):
        val = item.get(field)
        if val:
            return str(val)[:12]  # abbreviate long SHAs for readability
    return "unknown"


def _author_name(item: dict) -> str:
    """Extract the author name from a changeSet item."""
    author = item.get("author")
    if isinstance(author, dict):
        return author.get("fullName") or author.get("id") or "unknown"
    if isinstance(author, str):
        return author
    return item.get("authorEmail", "unknown")


def extract_changesets(build_data: dict) -> list[dict]:
    """
    Return a flat, normalized list of commits from a pruned build response.

    Each entry in the returned list has:
      - commit_id:  abbreviated hash / revision number
      - author:     display name of the committer
      - message:    full commit message (first 500 chars)
    """
    commits: list[dict] = []

    # Git puts an array of ChangeLogSet objects under "changeSets"
    change_sets_raw = build_data.get("changeSets")
    if change_sets_raw and isinstance(change_sets_raw, list):
        for change_set in change_sets_raw:
            for item in change_set.get("items") or []:
                commits.append(_normalize(item))
        return commits

    # SVN / Mercurial / Perforce use singular "changeSet"
    change_set_raw = build_data.get("changeSet")
    if change_set_raw and isinstance(change_set_raw, dict):
        for item in change_set_raw.get("items") or []:
            commits.append(_normalize(item))

    return commits


def _normalize(item: dict) -> dict:
    paths = item.get("affectedPaths") or item.get("paths") or []
    if isinstance(paths, list) and paths and isinstance(paths[0], dict):
        paths = [p.get("file", "") for p in paths]
    return {
        "commit_id": _commit_id(item),
        "author": _author_name(item),
        "message": (item.get("comment") or item.get("msg") or "")[:500].strip(),
        "affected_paths": paths[:20],
    }
