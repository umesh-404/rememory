"""Walk a project and decide, for each file: index it or skip it, and as what.

The filters are ordered cheapest-first -- directory name, then filename, then
extension, then size, then content probe. A repo with a large node_modules is
rejected at the directory level, so we never stat the 200,000 files inside it.

Skips are counted by reason rather than silently swallowed. "Why isn't my file
in the index?" is the question you will actually ask, and `--explain` answers it
for a specific path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pathspec

from .config import Config, Project

# Suppress the console window Windows would otherwise create for child
# processes. Everything here runs from background jobs and GUI processes.
_NO_WINDOW = {"creationflags": 0x08000000} if sys.platform == "win32" else {}


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path  # absolute
    rel_path: str  # POSIX-style, relative to project root -- the stable id
    chunker: str  # code | docs | text
    language: str
    ts_language: str | None  # tree-sitter grammar name, if any
    doc_type: str | None  # readme | adr | openapi | ... (docs only)
    size: int
    mtime: float
    content_hash: str  # sha256 of bytes -- drives Phase 7 incremental updates


class Discovery:
    def __init__(self, config: Config, project: Project) -> None:
        self.cfg = config
        self.project = project
        self.skips: Counter[str] = Counter()
        self._ignore_dirs = set(config.discovery.ignore_dirs) | set(project.extra_ignore_dirs)
        self._gitignore = self._load_gitignore() if config.discovery.respect_gitignore else None

    # ------------------------------------------------------------------ setup
    def _load_gitignore(self) -> pathspec.PathSpec | None:
        """Parse the project's .gitignore using git's own matching semantics.

        We use `pathspec` rather than hand-rolling glob matching because
        .gitignore rules are deceptively subtle -- negation with `!`, anchoring
        with a leading `/`, directory-only patterns with a trailing `/`, and
        `**` spanning path separators. Reimplementing that is a well-known way
        to produce an index that is quietly missing files.
        """
        gi = self.project.root / ".gitignore"
        if not gi.exists():
            return None
        try:
            lines = gi.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)

    # ------------------------------------------------------------------- walk
    def walk(self) -> list[DiscoveredFile]:
        found: list[DiscoveredFile] = []
        roots = (
            [self.project.root / sub for sub in self.project.include_only]
            if self.project.include_only
            else [self.project.root]
        )

        for start in roots:
            if not start.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(start):
                # Pruning in place stops os.walk from descending. This is the
                # single most important performance decision in this module.
                dirnames[:] = [d for d in dirnames if d not in self._ignore_dirs]

                for filename in filenames:
                    full = Path(dirpath) / filename
                    result = self.classify(full)
                    if result is not None:
                        found.append(result)

        return found

    # --------------------------------------------------------------- classify
    def classify(self, path: Path, *, explain: bool = False) -> DiscoveredFile | None:
        """Return a DiscoveredFile, or None with the reason recorded in .skips."""

        def skip(reason: str) -> None:
            self.skips[reason] += 1
            if explain:
                print(f"  SKIP  {path}  ->  {reason}")

        try:
            rel = path.resolve().relative_to(self.project.root.resolve())
        except ValueError:
            skip("outside project root")
            return None
        rel_posix = rel.as_posix()

        # any ignored directory anywhere in the path (covers --explain, where we
        # did not arrive via the pruned walk)
        if self._ignore_dirs.intersection(rel.parts[:-1]):
            skip("in ignored directory")
            return None

        name = path.name.lower()
        if name in self.cfg.discovery.ignore_files:
            skip("ignored filename (lock/meta file)")
            return None

        if any(name.endswith(sfx) for sfx in self.cfg.discovery.ignore_suffixes):
            skip("generated-file suffix")
            return None

        ext = path.suffix.lower()
        if ext in self.cfg.discovery.ignore_extensions:
            skip("binary/media extension")
            return None

        if self._gitignore is not None and self._gitignore.match_file(rel_posix):
            skip("matched .gitignore")
            return None

        try:
            stat = path.stat()
        except OSError:
            skip("unreadable")
            return None

        if stat.st_size == 0:
            skip("empty file")
            return None
        if stat.st_size > self.cfg.discovery.max_file_bytes:
            skip("exceeds max_file_bytes")
            return None

        spec = self._language_spec(path, ext)
        if spec is None:
            skip(f"unknown file type ({ext or 'no extension'})")
            return None

        raw = self._read_bytes(path)
        if raw is None:
            skip("binary content")
            return None

        return DiscoveredFile(
            path=path,
            rel_path=rel_posix,
            chunker=spec["chunker"],
            language=spec["language"],
            ts_language=spec.get("ts"),
            doc_type=self._doc_type(rel_posix) if spec["chunker"] == "docs" else None,
            size=stat.st_size,
            mtime=stat.st_mtime,
            content_hash=hashlib.sha256(raw).hexdigest(),
        )

    def _language_spec(self, path: Path, ext: str) -> dict[str, str] | None:
        """Map a file to its chunker+language, by extension or by exact name."""
        langs = self.cfg.languages
        if ext in langs:
            return langs[ext]

        # Files whose *name* carries the type, with no useful extension.
        by_name = {
            "dockerfile": {"chunker": "text", "language": "dockerfile"},
            "makefile": {"chunker": "text", "language": "make"},
            "caddyfile": {"chunker": "text", "language": "caddy"},
            ".env.example": {"chunker": "text", "language": "dotenv"},
            ".env.sample": {"chunker": "text", "language": "dotenv"},
            "license": {"chunker": "docs", "language": "text"},
        }
        return by_name.get(path.name.lower())

    def _doc_type(self, rel_posix: str) -> str:
        lowered = rel_posix.lower()
        base = lowered.rsplit("/", 1)[-1]
        for rule in self.cfg.doc_classification:
            pat = rule["pattern"]
            target = lowered if "/" in pat else base
            if fnmatch.fnmatch(target, pat):
                return rule["type"]
        return "guide"

    def _read_bytes(self, path: Path) -> bytes | None:
        """Read a file, rejecting binaries by CONTENT rather than by extension.

        Extensions lie in both directions: a `.txt` can hold a binary dump, and
        plenty of real config files have no extension at all. A NUL byte in the
        first block is the same heuristic git uses, and it is reliable in
        practice for UTF-8 text.
        """
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        probe = raw[: self.cfg.discovery.binary_probe_bytes]
        if b"\x00" in probe:
            return None
        try:
            probe.decode("utf-8")
        except UnicodeDecodeError:
            # A multi-byte character may straddle the probe boundary; only treat
            # it as binary if the whole file also fails to decode.
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return raw


def git_commit(root: Path) -> str | None:
    """Current HEAD commit, or None if this is not a git repo.

    Stored per chunk so you can tell which revision a piece of knowledge came
    from -- and, later, diff two indexes. Absence is normal and not an error:
    not every project is under version control.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            # Without CREATE_NO_WINDOW, Windows gives this child its own console
            # window. It is called once per project on every sync -- including
            # the scheduled one every 30 minutes -- so a console flashed on
            # screen and vanished, repeatedly, for no visible reason.
            **_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None
