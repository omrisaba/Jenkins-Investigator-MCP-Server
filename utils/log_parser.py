"""
Priority-budgeted log extraction for Jenkins console output.

Strategy for get_error_log():
  1. Scan the FULL log for error signatures, classified by severity tier
     (CRITICAL > ERROR > WARNING).
  2. Detect the nearest Jenkins pipeline stage/phase for each match via a
     single-pass stage index (bisect lookup).
  3. Deduplicate: keep first occurrence of each unique failure, identified by
     a composite fingerprint (tier + exception token + stage + message prefix).
     Count subsequent repeats.
  4. Fill a line budget in priority order:
     a. First 5 lines of the raw log (build context / trigger info).
     b. CRITICAL-tier sections (chronological, first occurrence).
     c. ERROR-tier sections (chronological, first occurrence).
     d. WARNING-tier sections only if budget remains after all errors.
     e. Last 30 lines of the raw log (final build result).
  5. Sections too large for remaining budget are clipped (first/last lines
     with omission marker) rather than silently dropped.
  6. A hard output guard ensures the final result stays well below context
     window limits regardless of budget accounting edge cases.
  7. If no patterns match at all, fall back to the last 250 raw lines.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Budget constants
# ---------------------------------------------------------------------------

MAX_LINES = 250
HARD_LIMIT = 350
CONTEXT_LINES = 10
LOG_HEAD_LINES = 5
LOG_TAIL_LINES = 30
MIN_CLIP_LINES = 5

# ---------------------------------------------------------------------------
# Tiered severity patterns
# ---------------------------------------------------------------------------

_CRITICAL_PATTERN = re.compile(
    r"\bFATAL\b|SIGKILL|SIGSEGV|OutOfMemoryError|core dumped|BUILD FAILURE"
    r"|FAILURE:\s+Build failed",
    re.IGNORECASE,
)

_ERROR_PATTERN = re.compile(
    r"\bERROR\b|Exception\b|Traceback|npm ERR!|\bFAILED\b|"
    r"AssertionError|NullPointerException|\bkilled\b|"
    r"Caused by:|panic:",
    re.IGNORECASE,
)

_WARNING_PATTERN = re.compile(
    r"\bWARN(?:ING)?\b|\bDEPRECATED\b|\bUNSTABLE\b",
    re.IGNORECASE,
)

_PHASE_PATTERN = re.compile(
    r"\[Pipeline\]\s*\{\s*\((.+?)\)"
    r"|^\[INFO\]\s*---\s*(.+?)\s*---"
    r"|^\[INFO\]\s*Building\s+(.+)"
    r'|Stage\s+"(.+?)"'
    r"|\[Stage:\s*(.+?)\]"
    r"|Entering stage\s+(.+)",
    re.MULTILINE,
)

_EXCEPTION_TOKEN_RE = re.compile(
    r"(\w+(?:Error|Exception|Failure))"
    r"|(\bFATAL\b)"
    r"|(\bSIGKILL\b|\bSIGSEGV\b)"
    r"|(\bcore dumped\b)"
    r"|(\bBUILD FAILURE\b)"
    r"|(npm ERR!)"
    r"|(\bTraceback\b)"
    r"|(\bpanic:)"
    r"|(\bCaused by:)",
    re.IGNORECASE,
)

TIER_CRITICAL = "CRITICAL"
TIER_ERROR = "ERROR"
TIER_WARNING = "WARNING"

_TIER_RANK = {TIER_CRITICAL: 0, TIER_ERROR: 1, TIER_WARNING: 2}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class MatchedSection:
    tier: str
    start: int
    end: int
    key_lines: list[str]
    phase: str
    repeat_count: int = 1

    @property
    def line_count(self) -> int:
        return self.end - self.start + 1

    @property
    def fingerprint(self) -> str:
        """Composite identity for deduplication.

        Uses the most specific key line (longest after normalization),
        its exception token, and the pipeline stage to distinguish
        genuinely different failures that share generic keywords.
        """
        best = max(self.key_lines, key=len) if self.key_lines else ""
        token = _extract_exception_token(best)
        msg_prefix = best[:100]
        return f"{self.tier}|{token}|{self.phase}|{msg_prefix}"


# ---------------------------------------------------------------------------
# Stage indexing (single-pass + bisect lookup)
# ---------------------------------------------------------------------------


def _build_stage_index(lines: list[str]) -> list[tuple[int, str]]:
    """Scan lines once and return sorted (line_idx, stage_name) pairs."""
    index: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        m = _PHASE_PATTERN.search(line)
        if m:
            for g in m.groups():
                if g:
                    index.append((i, g.strip()))
                    break
    return index


def _resolve_stage(stage_index: list[tuple[int, str]], line_idx: int) -> str:
    """Find nearest prior stage for a given line index via bisect."""
    if not stage_index:
        return ""
    positions = [s[0] for s in stage_index]
    pos = bisect_right(positions, line_idx) - 1
    if pos < 0:
        return ""
    return stage_index[pos][1]


# ---------------------------------------------------------------------------
# Line classification & normalization
# ---------------------------------------------------------------------------


def _classify_line(line: str) -> str | None:
    if _CRITICAL_PATTERN.search(line):
        return TIER_CRITICAL
    if _ERROR_PATTERN.search(line):
        return TIER_ERROR
    if _WARNING_PATTERN.search(line):
        return TIER_WARNING
    return None


def _normalize_key(line: str) -> str:
    """Strip timestamps, ANSI codes, and leading whitespace for dedup."""
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    line = re.sub(r"^\d{4}[-/]\d{2}[-/]\d{2}[\sT]\d{2}:\d{2}:\d{2}[.,]?\d*\s*", "", line)
    line = re.sub(r"^\[\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\]\s*", "", line)
    return line.strip()


def _extract_exception_token(line: str) -> str:
    """Extract a specific exception/failure class name from a line."""
    m = _EXCEPTION_TOKEN_RE.search(line)
    if m:
        for g in m.groups():
            if g:
                return g
    return line[:60]


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _scan(
    lines: list[str],
    stage_index: list[tuple[int, str]],
    context_lines: int = CONTEXT_LINES,
) -> list[MatchedSection]:
    """Scan all lines, build context ranges, classify, and merge overlaps."""
    match_data: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        tier = _classify_line(line)
        if tier:
            match_data.append((i, tier, line))

    if not match_data:
        return []

    raw_ranges: list[tuple[int, int, str, str, int]] = []
    for idx, tier, raw_line in match_data:
        start = max(0, idx - context_lines)
        end = min(len(lines) - 1, idx + context_lines)
        raw_ranges.append((start, end, tier, raw_line, idx))

    merged: list[MatchedSection] = []
    for start, end, tier, raw_line, match_idx in raw_ranges:
        key = _normalize_key(raw_line)
        phase = _resolve_stage(stage_index, match_idx)

        if merged and start <= merged[-1].end + 1:
            prev = merged[-1]
            prev.end = max(prev.end, end)
            if key not in prev.key_lines:
                prev.key_lines.append(key)
            if _TIER_RANK.get(tier, 99) < _TIER_RANK.get(prev.tier, 99):
                prev.tier = tier
            prev.phase = phase or prev.phase
        else:
            merged.append(MatchedSection(
                tier=tier, start=start, end=end,
                key_lines=[key], phase=phase,
            ))

    return merged


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate(sections: list[MatchedSection]) -> list[MatchedSection]:
    """Keep first occurrence of each unique failure; count repeats."""
    seen: dict[str, int] = {}
    unique: list[MatchedSection] = []

    for section in sections:
        fp = section.fingerprint
        if fp in seen:
            unique[seen[fp]].repeat_count += 1
        else:
            seen[fp] = len(unique)
            unique.append(section)

    return unique


# ---------------------------------------------------------------------------
# Budget allocation & formatting
# ---------------------------------------------------------------------------


def _make_header(section: MatchedSection) -> str:
    phase_tag = f' | Stage: "{section.phase}"' if section.phase else ""
    repeat_tag = (
        f" [repeated {section.repeat_count} more times]"
        if section.repeat_count > 1 else ""
    )
    return f"--- {section.tier} near line {section.start + 1}{phase_tag}{repeat_tag} ---"


def _format_section(
    section: MatchedSection,
    lines: list[str],
    max_lines: int | None = None,
) -> str:
    """Format a section, clipping to max_lines if too large rather than dropping."""
    header = _make_header(section)
    snippet = lines[section.start : section.end + 1]
    full_cost = len(snippet) + 1

    if max_lines is None or full_cost <= max_lines:
        return header + "\n" + "\n".join(snippet)

    if max_lines < 3:
        return header

    available = max_lines - 2
    if available < MIN_CLIP_LINES:
        clipped = snippet[:max(1, available)]
        omitted = len(snippet) - len(clipped)
        clipped.append(f"    [...{omitted} lines omitted...]")
    else:
        top_n = (available + 1) // 2
        bottom_n = available - top_n
        omitted = len(snippet) - top_n - bottom_n
        clipped = list(snippet[:top_n])
        clipped.append(f"    [...{omitted} lines omitted...]")
        if bottom_n > 0:
            clipped.extend(snippet[-bottom_n:])

    return header + "\n" + "\n".join(clipped)


def _budget_fill(
    sections: list[MatchedSection],
    lines: list[str],
    budget: int,
) -> list[str]:
    """Fill the line budget by priority: CRITICAL -> ERROR -> WARNING.

    Sections that fit entirely are included as-is. Sections too large for the
    remaining budget are clipped rather than dropped, ensuring critical signals
    are never silently lost. WARNING-tier sections are only included after all
    CRITICAL and ERROR sections have been handled.
    """
    tier_order = [TIER_CRITICAL, TIER_ERROR, TIER_WARNING]
    output_parts: list[str] = []
    used = 0
    exhausted = False

    for tier in tier_order:
        if exhausted:
            break
        for section in sections:
            if section.tier != tier:
                continue
            remaining = budget - used
            if remaining < MIN_CLIP_LINES + 1:
                exhausted = True
                break
            formatted = _format_section(section, lines, max_lines=remaining)
            cost = len(formatted.splitlines())
            output_parts.append(formatted)
            used += cost

    return output_parts


# ---------------------------------------------------------------------------
# Public API (unchanged interface)
# ---------------------------------------------------------------------------


def truncate_tail(text: str, max_lines: int = MAX_LINES) -> str:
    """Return the last ``max_lines`` lines of text with a truncation notice."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[-max_lines:]
    notice = f"[Log truncated: showing last {max_lines} of {len(lines)} lines]\n"
    return notice + "\n".join(kept)


def get_error_log(
    text: str,
    *,
    max_lines: int = MAX_LINES,
    hard_limit: int = HARD_LIMIT,
    include_head: bool = True,
    include_tail: bool = True,
) -> str:
    """Priority-budgeted error extraction.

    Args:
        text: Raw console/stage log text.
        max_lines: Soft line budget for the output (default 250).
        hard_limit: Absolute output cap (default 350).
        include_head: Include the first 5 lines as context anchor.  Set to
            False for stage logs where the head is not diagnostically useful.
        include_tail: Include the last 30 lines as context anchor.
    """
    if not text.strip():
        return "[Console log is empty]"

    lines = text.splitlines()
    total_lines = len(lines)

    stage_index = _build_stage_index(lines)
    sections = _scan(lines, stage_index)

    if not sections:
        return (
            f"[No error patterns matched. Showing last {max_lines} lines of raw log]\n\n"
            + truncate_tail(text, max_lines)
        )

    deduped = _deduplicate(sections)

    n_critical = sum(1 for s in deduped if s.tier == TIER_CRITICAL)
    n_error = sum(1 for s in deduped if s.tier == TIER_ERROR)
    n_warning = sum(1 for s in deduped if s.tier == TIER_WARNING)
    n_dupes = sum(s.repeat_count - 1 for s in deduped)

    summary = (
        f"[Log analysis: {total_lines} total lines | "
        f"{n_critical} critical, {n_error} error, {n_warning} warning "
        f"(unique matches) | {n_dupes} duplicates collapsed]"
    )

    fixed_cost = 3
    parts = [summary, ""]

    if include_head:
        head = lines[:LOG_HEAD_LINES]
        head_block = "--- Log start (first 5 lines) ---\n" + "\n".join(head)
        fixed_cost += len(head_block.splitlines())
        parts.extend([head_block, ""])

    if include_tail:
        tail_start = max(0, total_lines - LOG_TAIL_LINES)
        tail = lines[tail_start:]
        tail_block = f"--- Log end (last {len(tail)} lines) ---\n" + "\n".join(tail)
        fixed_cost += len(tail_block.splitlines())
    else:
        tail_block = None

    section_budget = max_lines - fixed_cost

    if section_budget < MIN_CLIP_LINES + 1:
        return summary + "\n\n" + truncate_tail(text, max_lines)

    error_parts = _budget_fill(deduped, lines, section_budget)

    if not error_parts:
        return summary + "\n\n" + truncate_tail(text, max_lines)

    for ep in error_parts:
        parts.append(ep)
        parts.append("")

    if tail_block is not None:
        parts.append(tail_block)

    result = "\n".join(parts)

    result_lines = result.splitlines()
    if len(result_lines) > hard_limit:
        result = "\n".join(result_lines[:hard_limit])
        result += f"\n[Output truncated at {hard_limit} lines — hard safety limit]"

    return result
