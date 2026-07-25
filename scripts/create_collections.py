# /// script
# requires-python = ">=3.12"
# dependencies = ["qdrant-client>=1.12", "pyyaml>=6.0"]
# ///
"""Create (or verify) the Qdrant collections defined in config/collections.yaml.

Run with uv, which reads the inline dependency block above and builds a
throwaway environment automatically -- no venv to create or activate:

    uv run scripts\\create_collections.py            # create / verify
    uv run scripts\\create_collections.py --status   # report only, change nothing
    uv run scripts\\create_collections.py --recreate code docs
        ^ DESTROYS and rebuilds the named collections. Refuses to touch
          `memory`, which cannot be rebuilt from anything.

Design notes
------------
* IDEMPOTENT. Safe to run repeatedly. Existing collections are verified, not
  overwritten; missing payload indexes are added.
* It ASSERTS that the vector size and distance in collections.yaml match
  config/embedding.yaml. A mismatch there is the single most damaging bug
  possible in this system -- it produces no error at all, just silently
  meaningless search results -- so it is checked in code, not in a comment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from qdrant_client import QdrantClient, models

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
QDRANT_URL = "http://127.0.0.1:6333"

# Collections that must never be destroyed by tooling: they hold authored
# knowledge that no re-index can regenerate.
PROTECTED = {"memory"}


def load_configs() -> tuple[dict, dict]:
    embedding = yaml.safe_load((CONFIG_DIR / "embedding.yaml").read_text(encoding="utf-8"))
    collections = yaml.safe_load((CONFIG_DIR / "collections.yaml").read_text(encoding="utf-8"))
    return embedding, collections


def assert_consistent(embedding: dict, collections: dict) -> None:
    """Fail loudly if the collection schema contradicts the embedding model."""
    want_size = embedding["model"]["dimensions"]
    want_distance = embedding["model"]["distance"].lower()
    dense = collections["defaults"]["vectors"]["dense"]

    if dense["size"] != want_size:
        raise SystemExit(
            f"FATAL: collections.yaml dense.size={dense['size']} but "
            f"embedding.yaml model.dimensions={want_size}.\n"
            f"Vectors of the wrong size are rejected by Qdrant; worse, a wrong "
            f"MODEL of the right size would be accepted silently. Fix before proceeding."
        )
    if dense["distance"].lower() != want_distance:
        raise SystemExit(
            f"FATAL: distance mismatch -- collections.yaml={dense['distance']} "
            f"vs embedding.yaml={want_distance}."
        )
    if embedding["schema_version"] != collections["schema_version"]:
        raise SystemExit("FATAL: schema_version differs between the two config files.")


def build_field_schema(spec: dict):
    """Translate a payload-index entry from YAML into a qdrant-client schema."""
    kind = spec["type"]

    if kind == "keyword":
        # is_tenant tells Qdrant to physically group points by this field, so a
        # project-filtered search reads far fewer disk pages.
        return models.KeywordIndexParams(
            type=models.KeywordIndexType.KEYWORD,
            is_tenant=spec.get("is_tenant", False),
        )
    if kind == "text":
        return models.TextIndexParams(
            type=models.TextIndexType.TEXT,
            tokenizer=models.TokenizerType(spec.get("tokenizer", "word")),
            lowercase=spec.get("lowercase", True),
            min_token_len=spec.get("min_token_len"),
            max_token_len=spec.get("max_token_len"),
        )
    if kind == "integer":
        return models.PayloadSchemaType.INTEGER
    if kind == "datetime":
        return models.PayloadSchemaType.DATETIME
    if kind == "float":
        return models.PayloadSchemaType.FLOAT
    if kind == "bool":
        return models.PayloadSchemaType.BOOL

    raise ValueError(f"Unsupported payload index type: {kind!r}")


def ensure_collection(client: QdrantClient, name: str, spec: dict, defaults: dict) -> None:
    dense = defaults["vectors"]["dense"]

    if client.collection_exists(name):
        info = client.get_collection(name)
        existing = info.config.params.vectors
        # Named vectors come back as a dict keyed by vector name.
        actual = existing["dense"] if isinstance(existing, dict) else existing
        if actual.size != dense["size"]:
            raise SystemExit(
                f"FATAL: collection '{name}' already exists with vector size "
                f"{actual.size}, but config expects {dense['size']}.\n"
                f"Qdrant cannot resize a collection in place. Either revert the "
                f"config, or re-create the collection (and re-index)."
            )
        print(f"  [ok]      '{name}' exists ({info.points_count} points, dim {actual.size})")
    else:
        client.create_collection(
            collection_name=name,
            vectors_config={
                "dense": models.VectorParams(
                    size=dense["size"],
                    distance=models.Distance(dense["distance"]),
                    on_disk=dense.get("on_disk", False),
                )
            },
            sparse_vectors_config={
                sparse_name: models.SparseVectorParams(
                    modifier=models.Modifier(sparse_spec["modifier"])
                )
                for sparse_name, sparse_spec in defaults["sparse_vectors"].items()
            },
            hnsw_config=models.HnswConfigDiff(**defaults["hnsw_config"]),
            optimizers_config=models.OptimizersConfigDiff(**defaults["optimizers_config"]),
            on_disk_payload=defaults["on_disk_payload"],
        )
        print(f"  [CREATED] '{name}'")

    # Payload indexes: common ones plus this collection's own. Creating an
    # index that already exists is a no-op in Qdrant, so this stays idempotent.
    indexes = {**CONFIG_COMMON_INDEXES, **spec.get("payload_indexes", {})}
    for field, field_spec in indexes.items():
        client.create_payload_index(
            collection_name=name,
            field_name=field,
            field_schema=build_field_schema(field_spec),
            wait=True,
        )
    print(f"            {len(indexes)} payload indexes ensured")


def show_status(client: QdrantClient) -> None:
    names = sorted(c.name for c in client.get_collections().collections)
    if not names:
        print("No collections exist.")
        return
    print(f"{'collection':<12}{'points':>9}{'indexed':>9}{'status':>10}  indexed payload fields")
    for name in names:
        info = client.get_collection(name)
        fields = ", ".join(sorted(info.payload_schema)) or "-"
        # `vectors_count` was removed from CollectionInfo in newer clients;
        # indexed_vectors_count is the meaningful number anyway -- it shows how
        # many vectors have made it into the HNSW graph rather than still
        # sitting in an unindexed segment.
        print(
            f"{name:<12}{info.points_count or 0:>9}{info.indexed_vectors_count or 0:>9}"
            f"{str(info.status.value):>10}  {fields}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="report only, change nothing")
    parser.add_argument(
        "--recreate",
        nargs="+",
        metavar="NAME",
        default=[],
        help="DESTROY and rebuild these collections (never 'memory')",
    )
    args = parser.parse_args()

    embedding, collections = load_configs()
    assert_consistent(embedding, collections)

    global CONFIG_COMMON_INDEXES
    CONFIG_COMMON_INDEXES = collections["common_payload_indexes"]

    client = QdrantClient(url=QDRANT_URL, timeout=60)

    if args.status:
        show_status(client)
        return

    for name in args.recreate:
        if name in PROTECTED:
            raise SystemExit(
                f"REFUSED: '{name}' holds authored knowledge that cannot be "
                f"regenerated by re-indexing. Delete it by hand if you truly mean to."
            )
        if name not in collections["collections"]:
            raise SystemExit(f"Unknown collection: {name!r}")
        if client.collection_exists(name):
            client.delete_collection(name)
            print(f"  [DELETED] '{name}'")

    print(f"Qdrant at {QDRANT_URL}")
    print(f"Model {embedding['model']['name']} -> {embedding['model']['dimensions']}d "
          f"{embedding['model']['distance']}\n")

    for name, spec in collections["collections"].items():
        ensure_collection(client, name, spec, collections["defaults"])

    print("\nDone.\n")
    show_status(client)


if __name__ == "__main__":
    sys.exit(main())
