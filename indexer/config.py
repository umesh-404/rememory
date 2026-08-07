"""Load and validate the YAML configuration files.

Everything downstream reads config through here, so there is exactly one place
that knows the file layout and exactly one place that enforces the cross-file
invariants. Validation happens at load time and fails loudly: a config error
that surfaces halfway through a 20-minute index is far more expensive than one
that surfaces immediately.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .runtime import ollama_url as _ollama_url
from .runtime import qdrant_url as _qdrant_url

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def _read(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        hint = (
            "\nThis is your project registry -- copy config/projects.example.yaml "
            "to config/projects.yaml and add your projects."
            if name == "projects.yaml"
            else ""
        )
        raise SystemExit(f"Missing config file: {path}{hint}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class EmbeddingConfig:
    name: str
    dimensions: int
    distance: str
    max_context_tokens: int
    query_prefix: str
    document_prefix: str
    base_url: str
    keep_alive: str
    timeout: int
    batch_size: int
    schema_version: int


@dataclass(frozen=True)
class ChunkingConfig:
    max_chunk_chars: int
    min_chunk_chars: int
    overlap_lines: int
    contextual_header: bool


@dataclass(frozen=True)
class DiscoveryConfig:
    ignore_dirs: frozenset[str]
    ignore_files: frozenset[str]
    ignore_extensions: frozenset[str]
    ignore_suffixes: tuple[str, ...]
    max_file_bytes: int
    respect_gitignore: bool
    binary_probe_bytes: int


@dataclass(frozen=True)
class SparseConfig:
    enabled: bool
    split_identifiers: bool
    min_token_len: int
    max_tokens_per_chunk: int


@dataclass(frozen=True)
class Project:
    name: str
    root: Path
    description: str = ""
    extra_ignore_dirs: tuple[str, ...] = ()
    include_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class Config:
    embedding: EmbeddingConfig
    chunking: ChunkingConfig
    discovery: DiscoveryConfig
    sparse: SparseConfig
    languages: dict[str, dict[str, str]]
    doc_classification: list[dict[str, str]]
    projects: dict[str, Project]
    # Passed through as a raw dict: consumed only by memory_mcp/rerank.py,
    # which owns its own defaults. Absent section -> reranking disabled.
    reranker: dict = field(default_factory=dict)
    qdrant_url: str = field(default_factory=lambda: _qdrant_url())
    collections: dict[str, str] = field(
        default_factory=lambda: {"code": "code", "docs": "docs", "memory": "memory"}
    )


@lru_cache(maxsize=1)
def load_config() -> Config:
    emb = _read("embedding.yaml")
    idx = _read("indexing.yaml")
    coll = _read("collections.yaml")
    proj = _read("projects.yaml")

    # --- cross-file invariant -------------------------------------------
    # Vectors from different models are not comparable, and mixing them raises
    # no error -- it just silently degrades every search. So the version stamp
    # must agree across all three files before we write a single point.
    versions = {
        "embedding.yaml": emb["schema_version"],
        "collections.yaml": coll["schema_version"],
        "indexing.yaml": idx["schema_version"],
    }
    if len(set(versions.values())) != 1:
        raise SystemExit(f"FATAL: schema_version mismatch across configs: {versions}")

    dense = coll["defaults"]["vectors"]["dense"]
    if dense["size"] != emb["model"]["dimensions"]:
        raise SystemExit(
            f"FATAL: collections.yaml dense.size={dense['size']} != "
            f"embedding.yaml dimensions={emb['model']['dimensions']}"
        )

    m, p = emb["model"], emb["provider"]
    embedding = EmbeddingConfig(
        name=m["name"],
        dimensions=m["dimensions"],
        distance=m["distance"],
        max_context_tokens=m["max_context_tokens"],
        # The YAML block scalar strips the trailing newline after "Query:", but
        # the model was trained with a space there. Restore it explicitly rather
        # than relying on how an editor happened to save the file.
        query_prefix=m["query_prefix"] if m["query_prefix"].endswith(" ") else m["query_prefix"] + " ",  # noqa: E501
        document_prefix=m["document_prefix"],
        # Ports live in runtime.py (config/runtime.json + REMEMORY_* env), so
        # a stock base_url defers to ollama_url() -- otherwise changing
        # ollama_port would fix the app but silently break the indexer and
        # reranker, which read this field. An explicitly customised yaml value
        # is still honoured.
        base_url=(
            _ollama_url()
            if p.get("base_url", "").rstrip("/") in ("", "http://127.0.0.1:11434")
            else p["base_url"].rstrip("/")
        ),
        keep_alive=p["keep_alive"],
        timeout=p["request_timeout_seconds"],
        batch_size=emb["batching"]["size"],
        schema_version=emb["schema_version"],
    )

    d = idx["discovery"]
    discovery = DiscoveryConfig(
        ignore_dirs=frozenset(d["ignore_dirs"]),
        ignore_files=frozenset(x.lower() for x in d["ignore_files"]),
        ignore_extensions=frozenset(x.lower() for x in d["ignore_extensions"]),
        ignore_suffixes=tuple(d["ignore_suffixes"]),
        max_file_bytes=d["max_file_bytes"],
        respect_gitignore=d["respect_gitignore"],
        binary_probe_bytes=d["binary_probe_bytes"],
    )

    c = idx["chunking"]
    chunking = ChunkingConfig(
        max_chunk_chars=c["max_chunk_chars"],
        min_chunk_chars=c["min_chunk_chars"],
        overlap_lines=c["overlap_lines"],
        contextual_header=c["contextual_header"],
    )

    s = idx["sparse"]
    sparse = SparseConfig(
        enabled=s["enabled"],
        split_identifiers=s["split_identifiers"],
        min_token_len=s["min_token_len"],
        max_tokens_per_chunk=s["max_tokens_per_chunk"],
    )

    projects: dict[str, Project] = {}
    for name, spec in (proj.get("projects") or {}).items():
        # Relative roots resolve against the repo root (CONFIG_DIR's parent),
        # not the process cwd -- `root: .` must mean "this repo" no matter
        # where the indexer or MCP server was launched from.
        root = Path(spec["root"])
        if not root.is_absolute():
            root = (CONFIG_DIR.parent / root).resolve()
        if not root.exists():
            # A warning, not a failure: you may register a project that lives on
            # a drive that is not currently mounted. MUST go to stderr: this
            # module is imported by the MCP server, whose stdout carries
            # JSON-RPC -- a stdout warning here would corrupt the protocol.
            print(f"  ! project '{name}': root does not exist: {root}", file=sys.stderr)
        projects[name] = Project(
            name=name,
            root=root,
            description=(spec.get("description") or "").strip(),
            extra_ignore_dirs=tuple(spec.get("extra_ignore_dirs") or ()),
            include_only=tuple(spec.get("include_only") or ()),
        )

    return Config(
        embedding=embedding,
        chunking=chunking,
        discovery=discovery,
        sparse=sparse,
        languages={k.lower(): v for k, v in idx["languages"].items()},
        doc_classification=idx["doc_classification"],
        projects=projects,
        reranker=emb.get("reranker") or {},
    )
