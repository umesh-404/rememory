"""Secret redaction at ingestion -- the one place rememory's local-only story
has a hole.

Everything in rememory stays on this machine EXCEPT what a search returns:
that content enters Claude's context, and conversation transcripts do leave
the machine. So a hardcoded token in an indexed repo could travel. Industry
RAG ingestion practice is to redact secrets before they enter the store; we
do it before both embedding and storage, so a credential can never be
retrieved because it was never kept.

Design constraints:
* Precision over recall for GENERIC patterns (an assignment named api_key),
  because false positives eat real code. But well-known, high-confidence
  token FORMATS (AWS, GitHub, Slack, private key blocks) are unambiguous --
  those match aggressively.
* The replacement preserves a short prefix (e.g. `ghp_[REDACTED]`) so search
  can still FIND "where is the GitHub token configured" -- the location
  remains discoverable, the credential does not.
* Redaction happens on the raw source before chunking, so line numbers in
  citations stay correct (replacements never add or remove lines).
"""

from __future__ import annotations

import re

# High-confidence token formats. Each is (name, compiled pattern). These are
# format-anchored -- they identify the SECRET itself, not its variable name --
# so they can run on every file with essentially no false positives.
_TOKEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key", re.compile(r"\b(A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("openai-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}\b")),
    ("gcp-api-key", re.compile(r"\bAIza[A-Za-z0-9_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

# Private key blocks: redact the body, keep the BEGIN/END lines (line counts
# must not change; see module docstring). Applied line-wise below.
_KEY_BLOCK_BOUNDARY = re.compile(r"-----(BEGIN|END) [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")

# Generic assignment: `password = "..."`, `api_key: '...'`, `SECRET=...`.
# Requires a secret-ish name AND a quoted value of plausible secret length,
# which is what keeps false positives near zero.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?P<name>[A-Z0-9_.-]*(?:secret|passwd|password|api[_-]?key|access[_-]?key|
       auth[_-]?token|private[_-]?key|client[_-]?secret)[A-Z0-9_.-]*)
    (?P<sep>\s*[:=]\s*|\s*=>\s*)
    (?P<q>["'])(?P<value>[^"'\n]{8,})(?P=q)
    """,
)


def redact(source: str) -> tuple[str, int]:
    """Return (redacted_source, redaction_count). Never changes line count."""
    count = 0

    def token_sub(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        token = match.group(0)
        # Keep a recognisable prefix so the LOCATION stays searchable.
        return token[:4] + "[REDACTED]"

    for _, pattern in _TOKEN_PATTERNS:
        source = pattern.sub(token_sub, source)

    def assign_sub(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{match.group('name')}{match.group('sep')}{match.group('q')}[REDACTED]{match.group('q')}"  # noqa: E501

    source = _ASSIGNMENT.sub(assign_sub, source)

    # Private key blocks, line-wise so the line count is preserved exactly.
    if "PRIVATE KEY" in source:
        lines = source.split("\n")
        inside = False
        for i, line in enumerate(lines):
            boundary = _KEY_BLOCK_BOUNDARY.search(line)
            if boundary:
                inside = boundary.group(1) == "BEGIN"
                continue
            if inside and line.strip():
                lines[i] = "[REDACTED PRIVATE KEY MATERIAL]"
                count += 1
        source = "\n".join(lines)

    return source, count
