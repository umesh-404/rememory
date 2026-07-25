"""Service-free unit tests: chunkers, sparse vectors, secret redaction.

Needs NO Qdrant, NO Ollama, NO Docker -- this is the suite CI runs, and the
one contributors can run instantly:

    uv run tests/test_unit.py
"""

from __future__ import annotations

import sys

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {label}{f' -- {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def test_redaction() -> None:
    from indexer.redact import redact

    src = "\n".join([
        'API_KEY = "sk-proj-abcdef1234567890abcdef1234567890"',
        'github = "ghp_ABCDEFghijklMNOPQRstuvwx1234567890ab"',
        "aws = AKIAIOSFODNN7EXAMPLE",
        'password: "hunter2hunter2"',
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA7bq3vXKurtsFakeKeyMaterial",
        "-----END RSA PRIVATE KEY-----",
        "normal_code = compute(x) + 1",
        'label = "not-a-secret"',
    ])
    out, n = redact(src)
    check("openai-style key redacted", "sk-proj-abcdef" not in out)
    check("github token redacted", "ghp_ABCDEF" not in out and "ghp_[REDACTED]" in out)
    check("aws key redacted", "AKIAIOSFODNN7EXAMPLE" not in out)
    check("password assignment redacted", "hunter2" not in out)
    check("private key body redacted", "FakeKeyMaterial" not in out)
    check("BEGIN/END boundaries kept", "BEGIN RSA PRIVATE KEY" in out)
    check("normal code untouched", "normal_code = compute(x) + 1" in out)
    check("short strings untouched", '"not-a-secret"' in out)
    check("line count preserved", out.count("\n") == src.count("\n"))
    check("redaction count sane", n >= 5, f"n={n}")


def test_code_chunker() -> None:
    from indexer.chunkers.code import CodeChunker

    c = CodeChunker(max_chars=6000, min_chars=40, overlap_lines=8)

    py = (
        "import os\n\n"
        "@app.get('/things')\n"
        "async def list_things(q: str) -> list[dict]:\n"
        '    """Return the things matching q, newest first."""\n'
        "    return await repo.find(q)\n\n"
        "class Widget:\n"
        '    """A widget with enough docstring text to clear the minimum chunk size."""\n'
        "    def spin(self, times: int) -> int:\n"
        "        return times * self.rate\n"
    )
    chunks = c.chunk(py, "python")
    names = {ch.symbol_name for ch in chunks}
    check("decorated function named", "list_things" in names, str(names))
    check("class chunk exists (docstring header)", "Widget" in names)
    check("method qualified", "Widget.spin" in names)
    route = next(ch for ch in chunks if ch.symbol_name == "list_things")
    check("decorator kept with function", "@app.get" in route.content)
    check("function body intact", "repo.find" in route.content)

    tsx = (
        "interface P { a: string; onSelect: (id: string) => void }\n"
        "export default function Feed({a}: P) {\n"
        "  return <div className='feed'>{a}</div>;\n}\n"
    )
    tchunks = c.chunk(tsx, "tsx")
    tnames = {ch.symbol_name for ch in tchunks}
    check("tsx parses with symbols", {"P", "Feed"} <= tnames, str(tnames))

    # small leaves must survive into module leftovers, not vanish
    small = "def a(x):\n    return x + 1\n\ndef b(x):\n    return x - 1\n"
    schunks = c.chunk(small, "python")
    everything = "\n".join(ch.content for ch in (schunks or []))
    check("small symbols not lost", "def a(x)" in everything and "def b(x)" in everything)


def test_docs_chunker() -> None:
    from indexer.chunkers.docs import DocsChunker

    d = DocsChunker(max_chars=6000, min_chars=40, overlap_lines=8)
    md = (
        "# Guide\n\nIntro paragraph with enough words to be its own chunk here.\n\n"
        "## Setup\n\nHow to set things up, with sufficient text to stand alone.\n\n"
        "```bash\n# this hash is a comment, not a heading\necho hi\n```\n\n"
        "### Docker\n\nDocker-specific setup notes that also clear the minimum.\n"
    )
    chunks = d.chunk(md)
    trails = [ch.heading_path for ch in chunks if ch.heading_path]
    check("heading trail nests", any(t == "Guide > Setup > Docker" for t in trails), str(trails))
    check("fence-protected hash not a heading",
          not any("this hash" in (ch.heading_path or "") for ch in chunks))


def test_sparse_determinism() -> None:
    from indexer.config import SparseConfig
    from indexer.sparse import build_sparse_vector

    cfg = SparseConfig(enabled=True, split_identifiers=True, min_token_len=2,
                       max_tokens_per_chunk=512)
    a = build_sparse_vector("the UserRepository handles VoiceRouter dispatch", cfg)
    b = build_sparse_vector("the UserRepository handles VoiceRouter dispatch", cfg)
    check("sparse vectors deterministic", a == b)
    check("identifier split present", len(a[0]) > 3, f"{len(a[0])} tokens")


def main() -> int:
    print("unit tests (no services required)\n")
    test_redaction()
    test_code_chunker()
    test_docs_chunker()
    test_sparse_determinism()
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All checks passed.'}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
