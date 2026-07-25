"""Symbol-aware code chunking with tree-sitter.

Why tree-sitter
---------------
The alternatives, and why they lose:

* Fixed character windows -- cut functions in half. A body without a signature
  cannot be found; a signature without a body is not worth finding.
* Regex ("split on `^def `") -- breaks on decorators, nested classes, generics,
  multi-line signatures, and every language that is not Python.
* Python's built-in `ast` -- excellent, but Python only. This machine also has
  TypeScript, SQL and shell to index.
* LangChain / LlamaIndex code splitters -- wrap tree-sitter anyway, and drag in
  a large framework we would otherwise never load.

tree-sitter gives a real parse tree for 100+ languages from prebuilt wheels (no
C compiler required), and -- crucially -- it is ERROR TOLERANT. It was built for
editors, so a file that does not compile still yields a usable tree. Code you
are midway through writing is exactly the code you most want to search.

What we extract
---------------
Top-level definitions become chunks. Class bodies are descended into so each
method is its own chunk (a 600-line class is not one idea), while the class's
own signature and docstring are kept as a separate chunk so "what is this class
for?" still has something to match. Everything outside a definition -- imports,
module constants, route registrations -- is collected into a `module` chunk,
because that is where a project's wiring lives.
"""

from __future__ import annotations

from . import Chunk
from .text import chunk_text

# Node types that represent a nameable definition, per grammar. Grammars differ
# in naming, so this is a lookup rather than a guess.
DEFINITION_NODES: dict[str, dict[str, str]] = {
    "python": {
        "function_definition": "function",
        "class_definition": "class",
        "decorated_definition": "decorated",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "method_definition": "method",
        "lexical_declaration": "const",  # `const Foo = () => {}` -- very common in TS
    },
    "javascript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "lexical_declaration": "const",
    },
    # tsx is a SEPARATE grammar from typescript in tree-sitter (JSX changes the
    # syntax), but its definition nodes are the same. Missing this entry made
    # every React component fall back to line-window chunking.
    "tsx": {
        "function_declaration": "function",
        "class_declaration": "class",
        "interface_declaration": "interface",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
        "method_definition": "method",
        "lexical_declaration": "const",
    },
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_declaration": "type",
    },
    "rust": {
        "function_item": "function",
        "struct_item": "struct",
        "enum_item": "enum",
        "trait_item": "trait",
        "impl_item": "impl",
        "mod_item": "module",
    },
    "java": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "enum_declaration": "enum",
    },
    "csharp": {
        "class_declaration": "class",
        "interface_declaration": "interface",
        "method_declaration": "method",
        "struct_declaration": "struct",
        "record_declaration": "record",
    },
    "c": {"function_definition": "function", "struct_specifier": "struct"},
    "cpp": {
        "function_definition": "function",
        "class_specifier": "class",
        "struct_specifier": "struct",
    },
    "ruby": {"method": "method", "class": "class", "module": "module"},
    "php": {
        "function_definition": "function",
        "class_declaration": "class",
        "method_declaration": "method",
    },
    "sql": {
        "create_table": "table",
        "create_view": "view",
        "create_function": "function",
        "statement": "statement",
    },
    "bash": {"function_definition": "function"},
    "css": {"rule_set": "rule", "media_statement": "media"},
    "scss": {"rule_set": "rule", "mixin_statement": "mixin"},
    "html": {"element": "element"},
}

# Node types whose children should be walked into rather than taken whole:
# a class is a container of methods, not a single idea.
CONTAINER_NODES = {
    "class_definition",
    "class_declaration",
    "class_specifier",
    "class",
    "impl_item",
    "trait_item",
    "mod_item",
    "module",
}
# Deliberately NOT containers:
# - decorated_definition: whether it is a container depends on what is INSIDE
#   the decorator (a decorated class is; a decorated function is not), so
#   _walk decides from the inner definition's type. Listing it here sent
#   decorated FUNCTIONS down the container path: only their decorator+signature
#   header was stored and their body leaked into module leftovers.
# - export_statement: not in DEFINITION_NODES at all. It is a transparent
#   wrapper -- _walk recurses into non-definition nodes, so the inner
#   function/class/const is found with its real name. Listing it as a
#   definition made every exported React component an anonymous "export" chunk.
# - interface_declaration: an interface is one idea. Treating it as a container
#   split off an empty header and scattered its members into module leftovers.


def _node_name(node) -> str | None:
    """Best-effort identifier for a definition node.

    `child_by_field_name("name")` covers most grammars. Where it does not
    (TypeScript's `const Foo = ...`, decorators), we fall back to the first
    identifier-ish child.
    """
    field = node.child_by_field_name("name")
    if field is not None:
        return field.text.decode("utf-8", "replace")

    for child in node.children:
        if child.type in {
            "identifier",
            "type_identifier",
            "property_identifier",
            "word",
            "constant",
        }:
            return child.text.decode("utf-8", "replace")
        # `const handler = () => {}`
        if child.type == "variable_declarator":
            inner = child.child_by_field_name("name")
            if inner is not None:
                return inner.text.decode("utf-8", "replace")
    return None


def _body_node(node):
    """The block/body of a definition, used to separate a container's own
    header (signature + docstring) from its members."""
    for field in ("body", "block"):
        found = node.child_by_field_name(field)
        if found is not None:
            return found
    return None


class CodeChunker:
    def __init__(self, *, max_chars: int, min_chars: int, overlap_lines: int) -> None:
        self.max_chars = max_chars
        self.min_chars = min_chars
        self.overlap_lines = overlap_lines

    def chunk(self, source: str, ts_language: str) -> list[Chunk] | None:
        """Return chunks, or None if this language cannot be parsed here.

        None (rather than an exception) signals the caller to fall back to the
        line-window chunker. Failing to parse must degrade quality, not lose
        the file.
        """
        try:
            from tree_sitter_language_pack import get_parser
        except ImportError:
            return None

        definitions = DEFINITION_NODES.get(ts_language)
        if definitions is None:
            return None

        try:
            parser = get_parser(ts_language)
            tree = parser.parse(source.encode("utf-8"))
        except Exception:
            return None

        lines = source.splitlines()
        chunks: list[Chunk] = []
        # Lines claimed by a definition; whatever is left is module-level glue.
        claimed: set[int] = set()

        self._walk(tree.root_node, definitions, lines, chunks, claimed, prefix="")

        # ---- module-level remainder: imports, constants, app wiring --------
        leftover = [i for i in range(len(lines)) if i not in claimed and lines[i].strip()]
        if leftover:
            # Preserve original line numbers by keeping runs contiguous.
            runs: list[list[int]] = []
            for i in leftover:
                if runs and i == runs[-1][-1] + 1:
                    runs[-1].append(i)
                else:
                    runs.append([i])
            # Adjacent runs are grouped up to max_chars. We report the group's
            # true span (first line of the first run -> last line of the last),
            # which is a superset of the text it holds. Joining ALL runs into
            # one blob and labelling it with a single range would report line
            # numbers that simply do not correspond to the content -- and a
            # wrong file:line is worse than a coarse one, because Claude will
            # cite it.
            group: list[list[int]] = []
            group_chars = 0

            def flush(group: list[list[int]], _chunks=chunks) -> None:
                if not group:
                    return
                text = "\n".join("\n".join(lines[r[0] : r[-1] + 1]) for r in group)
                if len(text.strip()) < self.min_chars:
                    return
                for part in chunk_text(
                    text,
                    max_chars=self.max_chars,
                    min_chars=self.min_chars,
                    overlap_lines=self.overlap_lines,
                    symbol_type="module",
                    symbol_name=None,
                    start_line_offset=group[0][0],
                ):
                    # Clamp to the group's real span.
                    part.end_line = min(part.end_line, group[-1][-1] + 1)
                    _chunks.append(part)

            for run in runs:
                run_chars = sum(len(lines[i]) + 1 for i in run)
                if group and group_chars + run_chars > self.max_chars:
                    flush(group)
                    group, group_chars = [], 0
                group.append(run)
                group_chars += run_chars
            flush(group)

        if not chunks:
            return None  # parsed, but found nothing -- let the fallback try

        chunks.sort(key=lambda c: c.start_line)
        return chunks

    def _walk(self, node, definitions, lines, chunks, claimed, prefix: str) -> None:
        for child in node.children:
            kind = definitions.get(child.type)
            if kind is None:
                # Not a definition itself, but may contain some (e.g. a TS
                # `export_statement` wrapping a class).
                if child.child_count:
                    self._walk(child, definitions, lines, chunks, claimed, prefix)
                continue

            # A decorated_definition node has no name of its own -- the name
            # and true kind live on the definition INSIDE it. Without this,
            # every @app.get route and @decorator'd function indexes as an
            # anonymous "decorated" chunk, which makes exact-symbol search
            # useless for exactly the functions (routes, tools, fixtures)
            # people search for most. The chunk still spans the whole node so
            # decorators stay attached to their function.
            name_node = child
            if child.type == "decorated_definition":
                inner = child.child_by_field_name("definition")
                if inner is not None:
                    name_node = inner
                    kind = definitions.get(inner.type, kind)

            name = _node_name(name_node)
            qualified = f"{prefix}{name}" if name else prefix.rstrip(".") or None

            if name_node.type in CONTAINER_NODES:
                body = _body_node(name_node)
                if body is not None:
                    # The container's own header: decorators + signature +
                    # docstring, minus its members. Answers "what is this class
                    # for?". The docstring is syntactically the body's first
                    # statement, so without pulling it in the header is often
                    # just `class X:` -- too small to store, and the class
                    # would have no symbol chunk at all.
                    header_end = body.start_point[0]
                    # In tree-sitter's python grammar the docstring is a bare
                    # `string` in class blocks but an expression_statement
                    # wrapping a string in function blocks -- accept both.
                    first = body.children[0] if body.children else None
                    is_docstring = first is not None and (
                        first.type == "string"
                        or (
                            first.type == "expression_statement"
                            and first.children
                            and first.children[0].type == "string"
                        )
                    )
                    if is_docstring:
                        header_end = first.end_point[0] + 1
                    header = "\n".join(lines[child.start_point[0] : header_end])
                    if len(header.strip()) >= self.min_chars:
                        chunks.append(
                            Chunk(
                                content=header,
                                start_line=child.start_point[0] + 1,
                                end_line=header_end,
                                symbol_type=kind,
                                symbol_name=qualified,
                            )
                        )
                        claimed.update(range(child.start_point[0], header_end))
                    self._walk(
                        body,
                        definitions,
                        lines,
                        chunks,
                        claimed,
                        prefix=f"{qualified}." if qualified else prefix,
                    )
                    continue

            # A leaf definition: take it whole.
            start, end = child.start_point[0], child.end_point[0]
            text = "\n".join(lines[start : end + 1])

            if len(text) <= self.max_chars:
                if len(text.strip()) >= self.min_chars:
                    claimed.update(range(start, end + 1))
                    chunks.append(
                        Chunk(
                            content=text,
                            start_line=start + 1,
                            end_line=end + 1,
                            symbol_type=kind,
                            symbol_name=qualified,
                        )
                    )
                # else: deliberately NOT claimed. A symbol too small to stand
                # alone (a one-line getter, a tiny CSS rule) falls through to
                # the module-leftover collector and survives grouped with its
                # neighbours. Claiming before this check silently DELETED such
                # symbols from the index -- neither stored nor left over.
            else:
                claimed.update(range(start, end + 1))
                # An oversized function. Split it, but keep the symbol metadata
                # on every piece so all parts remain attributable.
                chunks.extend(
                    chunk_text(
                        text,
                        max_chars=self.max_chars,
                        min_chars=self.min_chars,
                        overlap_lines=self.overlap_lines,
                        symbol_type=kind,
                        symbol_name=qualified,
                        start_line_offset=start,
                    )
                )
