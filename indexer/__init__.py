"""Indexing pipeline: files on disk -> chunks -> vectors -> Qdrant.

Module map (each does one job, so any one can be replaced without touching the
others):

    config.py      load + validate config/*.yaml into typed objects
    discovery.py   walk a project, apply ignore rules, decide file type
    chunkers/      split file content into embeddable pieces
        code.py    tree-sitter, symbol-aware (functions, classes, ...)
        docs.py    markdown, heading-aware
        text.py    line-window fallback for everything else
    sparse.py      term-frequency vectors for hybrid search
    embedder.py    batched calls to Ollama
    store.py       Qdrant upsert / delete, deterministic point ids
    pipeline.py    orchestration
    cli.py         command line entry point
"""

__version__ = "0.4.0"
