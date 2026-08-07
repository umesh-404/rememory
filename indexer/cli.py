"""Command line interface.

    uv run -m indexer.cli index --project my-app      # index one project
    uv run -m indexer.cli index --all                   # every registered project
    uv run -m indexer.cli index --project x --changed    # skip unchanged files
    uv run -m indexer.cli index --project x --reset      # wipe project first
    uv run -m indexer.cli search "how do we refresh tokens" --project my-app
    uv run -m indexer.cli status
    uv run -m indexer.cli explain path\\to\\file.py --project my-app
    uv run -m indexer.cli chunks path\\to\\file.py --project my-app

`explain` and `chunks` exist because the two questions you will actually ask are
"why isn't my file indexed?" and "is it being split sensibly?" -- and neither is
answerable by staring at a vector database.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from .config import Config, load_config
from .discovery import Discovery
from .embedder import Embedder, EmbeddingError
from .pipeline import Pipeline
from .store import Store

console = Console()


def _projects(cfg: Config, args) -> list:
    if args.all:
        return list(cfg.projects.values())
    if not args.project:
        raise SystemExit("Specify --project NAME or --all")
    if args.project not in cfg.projects:
        raise SystemExit(
            f"Unknown project '{args.project}'. Known: {', '.join(cfg.projects) or '(none)'}\n"
            f"Add it to config/projects.yaml"
        )
    return [cfg.projects[args.project]]


def _one_project(cfg: Config, name: str):
    """Friendly lookup for commands that take a single required --project."""
    if name not in cfg.projects:
        raise SystemExit(
            f"Unknown project '{name}'. Known: {', '.join(cfg.projects) or '(none)'}\n"
            f"Add it to config/projects.yaml"
        )
    return cfg.projects[name]


def cmd_index(cfg: Config, args) -> int:
    # Single-writer lock shared with the MCP sync_index tool -- see
    # indexer/lockfile.py for why every indexing path goes through it.
    from .lockfile import holder_age_seconds, index_lock

    with index_lock() as acquired:
        if not acquired:
            age = holder_age_seconds() or 0.0
            console.print(
                f"[yellow]Another index/sync is already running[/] "
                f"({age:.0f}s old). Exiting; nothing to do."
            )
            return 0
        return _cmd_index_locked(cfg, args)


def _cmd_index_locked(cfg: Config, args) -> int:
    store = Store(cfg)
    store.verify()
    pipeline = Pipeline(cfg)

    with Embedder(cfg.embedding) as embedder:
        try:
            console.print(f"[dim]model:[/] {embedder.health()}  [dim]dims:[/] {cfg.embedding.dimensions}")  # noqa: E501
        except EmbeddingError as exc:
            console.print(f"[red]{exc}[/]")
            return 1

        for project in _projects(cfg, args):
            if not project.root.exists():
                console.print(f"[yellow]skip[/] {project.name}: root missing ({project.root})")
                continue

            if args.reset:
                # Only ever touches derived collections. `memory` is untouchable
                # from here by construction -- see Store.delete_project.
                for coll in ("code", "docs"):
                    store.delete_project(coll, project.name)
                console.print(f"[yellow]reset[/] cleared code+docs for '{project.name}'")

            started = time.perf_counter()
            with Progress(
                SpinnerColumn(),
                TextColumn("{task.description}"),
                BarColumn(),
                TextColumn("{task.completed}/{task.total}"),
                TimeElapsedColumn(),
                console=console,
            ) as progress:
                stats = pipeline.index_project(
                    project,
                    store,
                    embedder,
                    only_changed=args.changed,
                    progress=progress,
                    limit=args.limit,
                )
            elapsed = time.perf_counter() - started

            console.print(
                f"[green]{project.name}[/]: {stats.files_indexed} files -> "
                f"{stats.chunks} chunks in {elapsed:.1f}s "
                f"({stats.chunks / elapsed:.1f} chunks/s)"
            )
            detail = [f"{k}={v}" for k, v in sorted(stats.by_collection.items())]
            console.print(
                f"  [dim]{' '.join(detail)}  seen={stats.files_seen} "
                f"unchanged={stats.files_skipped_unchanged} "
                f"deleted={stats.files_deleted} renamed={stats.files_renamed} "
                f"fallback_parse={stats.fallback_parses} failed={stats.files_failed} "
                f"secrets_redacted={stats.secrets_redacted}[/]"
            )
            if stats.errors:
                for err in stats.errors[:10]:
                    console.print(f"  [red]![/] {err}")
                if len(stats.errors) > 10:
                    console.print(f"  [red]![/] ... and {len(stats.errors) - 10} more")

    return 0


def cmd_search(cfg: Config, args) -> int:
    # Uses the SAME hybrid Searcher as the MCP server, so what you see in the
    # terminal is exactly what Claude gets. A separate dense-only path here
    # (the original implementation) meant CLI smoke tests were testing a
    # different retrieval pipeline than the one in production.
    from memory_mcp.search import Searcher

    hits = Searcher(cfg).search(
        args.collection,
        args.query,
        project=args.project,
        limit=args.limit,
        filters={"language": args.language} if args.language else None,
    )

    if not hits:
        console.print("[yellow]No results.[/]")
        return 0

    for hit in hits:
        location = (
            f"{hit.source_path}:{hit.start_line}-{hit.end_line}"
            if hit.start_line
            else hit.source_path
        )
        # Results are ORDERED by the cross-encoder when reranking ran, so show
        # that score; hit.score is the raw RRF fusion value and printing it
        # made the score column look unsorted.
        shown = hit.extra.get("rerank", hit.score)
        console.print(f"\n[bold cyan]{shown:.3f}[/] [green]{location}[/] [dim]{hit.symbol or ''}[/]")  # noqa: E501
        body = hit.content.strip().splitlines()
        for line in body[: args.context]:
            console.print(f"  [dim]|[/] {line[:140]}")
        if len(body) > args.context:
            console.print(f"  [dim]| ... {len(body) - args.context} more lines[/]")
    console.print()
    return 0


def cmd_status(cfg: Config, args) -> int:
    store = Store(cfg)
    table = Table(title="Indexed content")
    table.add_column("project")
    table.add_column("code", justify="right")
    table.add_column("docs", justify="right")
    table.add_column("memory", justify="right")

    for name in cfg.projects:
        s = store.project_stats(name)
        table.add_row(name, str(s.get("code", 0)), str(s.get("docs", 0)), str(s.get("memory", 0)))

    total = store.project_stats(None)
    table.add_section()
    table.add_row(
        "[bold]TOTAL (all)[/]",
        f"[bold]{total.get('code', 0)}[/]",
        f"[bold]{total.get('docs', 0)}[/]",
        f"[bold]{total.get('memory', 0)}[/]",
    )
    console.print(table)
    return 0


def cmd_explain(cfg: Config, args) -> int:
    """Answer 'why isn't this file in the index?' for one specific path."""
    project = _one_project(cfg, args.project)
    discovery = Discovery(cfg, project)
    path = Path(args.path).resolve()
    result = discovery.classify(path, explain=True)
    if result is None:
        console.print("[red]Not indexed[/] (reason above)")
        return 1
    console.print(
        f"[green]Indexed[/] as [bold]{result.chunker}[/] "
        f"({result.language}, ts={result.ts_language}, doc_type={result.doc_type})\n"
        f"  rel_path={result.rel_path}\n  size={result.size}B  hash={result.content_hash[:16]}..."
    )
    return 0


def cmd_chunks(cfg: Config, args) -> int:
    """Show how a file would be split, without embedding or storing anything."""
    project = _one_project(cfg, args.project)
    discovery = Discovery(cfg, project)
    disc = discovery.classify(Path(args.path).resolve(), explain=True)
    if disc is None:
        return 1

    pipeline = Pipeline(cfg)
    source = disc.path.read_text(encoding="utf-8", errors="replace")
    chunks, fallback = pipeline.chunk_file(disc, source)

    console.print(
        f"{len(chunks)} chunks from {disc.rel_path} "
        f"[dim](chunker={disc.chunker}{', FALLBACK' if fallback else ''})[/]\n"
    )
    for c in chunks:
        header = pipeline.header_for(disc, c)
        console.print(
            f"[cyan]L{c.start_line}-{c.end_line}[/] "
            f"[magenta]{c.symbol_type or '-'}[/] [bold]{c.symbol_name or c.heading_path or ''}[/] "
            f"[dim]({len(c.content)} chars)[/]"
        )
        if args.verbose:
            console.print(f"  [dim]embed header:[/] {header}")
            for line in c.content.splitlines()[:6]:
                console.print(f"  [dim]|[/] {line[:120]}")
            console.print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="memory-index", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "sync",
        help="incremental update of ALL projects: changed files re-indexed, "
        "deleted files purged. The command automation should run.",
    )
    p.set_defaults(func=cmd_index, all=True, project=None, changed=True, reset=False, limit=None)

    p = sub.add_parser("index", help="index one or all projects")
    p.add_argument("--project")
    p.add_argument("--all", action="store_true")
    p.add_argument("--changed", action="store_true", help="skip files whose content hash is unchanged")  # noqa: E501
    p.add_argument("--reset", action="store_true", help="wipe this project's code+docs first")
    p.add_argument("--limit", type=int, help="index at most N files (for trying things out)")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="semantic search (a smoke test; Claude uses MCP)")
    p.add_argument("query")
    p.add_argument("--project")
    p.add_argument("--collection", default="code", choices=["code", "docs", "memory"])
    p.add_argument("--language")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--context", type=int, default=8, help="lines of each hit to print")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("status", help="point counts per project")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("explain", help="why is / isn't this file indexed?")
    p.add_argument("path")
    p.add_argument("--project", required=True)
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("chunks", help="preview how a file would be chunked")
    p.add_argument("path")
    p.add_argument("--project", required=True)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_chunks)

    args = parser.parse_args()
    cfg = load_config()
    return args.func(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
