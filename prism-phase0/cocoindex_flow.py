#!/usr/bin/env python3
"""
Prism Phase 0 — cocoindex_flow.py

Defines a CocoIndex flow that embeds the prism-phase0 corpus
(fixtures/corpora/**) into a PostgreSQL vector index.

Usage:
    # Build / update the index
    python cocoindex_flow.py update

    # Interactive semantic search (sanity check)
    python cocoindex_flow.py search

    # Python API (used by baseline.py BL-C real mode)
    from cocoindex_flow import search_code
    results = search_code("authenticate user JWT", top_k=5)
"""

import argparse
import functools
import os
from pathlib import Path
from typing import Any

import cocoindex
import numpy as np
from dotenv import load_dotenv
from numpy.typing import NDArray
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------------------
# Corpus root — fixtures/corpora under this file's directory
# ---------------------------------------------------------------------------
_HERE = Path(__file__).parent
CORPUS_ROOT = str(_HERE / "fixtures" / "corpora")

TOP_K_DEFAULT = 5


# ---------------------------------------------------------------------------
# Reusable embedding transform
# ---------------------------------------------------------------------------
@cocoindex.transform_flow()
def code_to_embedding(
    text: cocoindex.DataSlice[str],
) -> cocoindex.DataSlice[NDArray[np.float32]]:
    """Embed a code snippet using a local SentenceTransformer model."""
    return text.transform(
        cocoindex.functions.SentenceTransformerEmbed(
            model="sentence-transformers/all-MiniLM-L6-v2"
        )
    )


# ---------------------------------------------------------------------------
# Index flow
# ---------------------------------------------------------------------------
@cocoindex.flow_def(name="PrismCodeEmbedding")
def prism_code_embedding_flow(
    flow_builder: cocoindex.FlowBuilder,
    data_scope: cocoindex.DataScope,
) -> None:
    """
    Index all Python and TypeScript files under fixtures/corpora into
    a Postgres pgvector table with cosine-similarity search support.
    """
    data_scope["files"] = flow_builder.add_source(
        cocoindex.sources.LocalFile(
            path=CORPUS_ROOT,
            included_patterns=["*.py", "*.ts"],
            excluded_patterns=["**/.*", "**/__pycache__", "**/node_modules"],
        )
    )

    code_embeddings = data_scope.add_collector()

    with data_scope["files"].row() as file:
        file["language"] = file["filename"].transform(
            cocoindex.functions.DetectProgrammingLanguage()
        )
        file["chunks"] = file["content"].transform(
            cocoindex.functions.SplitRecursively(),
            language=file["language"],
            chunk_size=1000,
            min_chunk_size=100,
            chunk_overlap=200,
        )
        with file["chunks"].row() as chunk:
            chunk["embedding"] = chunk["text"].call(code_to_embedding)
            code_embeddings.collect(
                filename=file["filename"],
                location=chunk["location"],
                code=chunk["text"],
                embedding=chunk["embedding"],
                start=chunk["start"],
                end=chunk["end"],
            )

    code_embeddings.export(
        "code_embeddings",
        cocoindex.targets.Postgres(),
        primary_key_fields=["filename", "location"],
        vector_indexes=[
            cocoindex.VectorIndexDef(
                field_name="embedding",
                metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Connection pool (cached)
# ---------------------------------------------------------------------------
@functools.cache
def _connection_pool() -> ConnectionPool:
    url = os.environ.get("COCOINDEX_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "COCOINDEX_DATABASE_URL is not set. "
            "Create prism-phase0/.env or export the variable."
        )
    return ConnectionPool(url)


# ---------------------------------------------------------------------------
# Query helper — used by baseline.py real BL-C mode
# ---------------------------------------------------------------------------
@prism_code_embedding_flow.query_handler(
    result_fields=cocoindex.QueryHandlerResultFields(
        embedding=["embedding"], score="score"
    )
)
def search(query: str, top_k: int = TOP_K_DEFAULT) -> cocoindex.QueryOutput:
    """
    Semantic search over the indexed corpus.
    Returns up to *top_k* chunks ranked by cosine similarity.
    """
    table_name = cocoindex.utils.get_target_default_name(
        prism_code_embedding_flow, "code_embeddings"
    )
    query_vector = code_to_embedding.eval(query)

    with _connection_pool().connection() as conn:
        register_vector(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT filename, code, embedding,
                       1.0 - (embedding <=> %s) AS score,
                       start, "end"
                FROM {table_name}
                ORDER BY score DESC
                LIMIT %s
                """,
                (query_vector, top_k),
            )
            rows = cur.fetchall()

    return cocoindex.QueryOutput(
        query_info=cocoindex.QueryInfo(
            embedding=query_vector,
            similarity_metric=cocoindex.VectorSimilarityMetric.COSINE_SIMILARITY,
        ),
        results=[
            {
                "filename": r[0],
                "code": r[1],
                "embedding": r[2],
                "score": float(r[3]),
                "start": r[4],
                "end": r[5],
            }
            for r in rows
        ],
    )


def search_code(
    query: str,
    top_k: int = TOP_K_DEFAULT,
) -> list[dict[str, Any]]:
    """
    Thin public wrapper around :func:`search` for use from baseline.py.

    Returns a list of dicts with keys:
        filename, code, score, start_line, end_line
    """
    output = search(query, top_k=top_k)
    results = []
    for r in output.results:
        start_line = r["start"].get("line", 0) if isinstance(r["start"], dict) else 0
        end_line = r["end"].get("line", 0) if isinstance(r["end"], dict) else 0
        results.append(
            {
                "filename": r["filename"],
                "code": r["code"],
                "score": r["score"],
                "start_line": start_line,
                "end_line": end_line,
            }
        )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_update() -> None:
    """Build / refresh the vector index from the corpus."""
    print(f"Indexing corpus at: {CORPUS_ROOT}")
    stats = prism_code_embedding_flow.update()
    print(f"Done. Stats: {stats}")


def _cmd_search() -> None:
    """Interactive search loop for manual sanity-checking."""
    print("CocoIndex semantic search — Prism Phase 0 corpus")
    print("Type a query and press Enter. Empty input exits.\n")
    while True:
        query = input("Query> ").strip()
        if not query:
            break
        results = search_code(query, top_k=TOP_K_DEFAULT)
        if not results:
            print("  (no results)\n")
            continue
        for i, r in enumerate(results, 1):
            print(
                f"  [{i}] score={r['score']:.4f}  "
                f"{r['filename']}  L{r['start_line']}-{r['end_line']}"
            )
            # Show first 3 lines of the chunk as a preview
            preview = "\n       ".join(r["code"].splitlines()[:3])
            print(f"       {preview}")
        print()


def main() -> None:
    load_dotenv()
    cocoindex.init()

    parser = argparse.ArgumentParser(description="Prism Phase 0 — CocoIndex flow CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="Build / refresh the vector index")
    sub.add_parser("search", help="Interactive semantic search")

    args = parser.parse_args()
    if args.cmd == "update":
        _cmd_update()
    elif args.cmd == "search":
        _cmd_search()


if __name__ == "__main__":
    main()
