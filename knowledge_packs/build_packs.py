#!/usr/bin/env python3
"""Build and verify Data.O.G v3.0 offline starter knowledge packs (.json.gz).

Usage:
    python3 knowledge_packs/build_packs.py
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

KB_ROOT = Path(__file__).resolve().parent
REPO_ROOT = KB_ROOT.parent
SOURCES_DIR = KB_ROOT / "sources"
PACKS_DIR = KB_ROOT / "packs"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.platform.context import PlatformContext  # noqa: E402


# Source document definitions: (dialect, relative_path, title, tags)
SOURCE_SPECS: List[Tuple[str, str, str, List[str]]] = [
    (
        "DWS",
        "dws/01_dws_9.1_sql_ddl_syntax.md",
        "华为云 GaussDB(DWS) 9.1 / 8.3 SQL 语法与 DDL 规范",
        ["DWS", "9.1", "8.3", "HSTORE_OPT", "Binlog", "DDL", "冷热表", "Recyclebin"],
    ),
    (
        "DWS",
        "dws/02_dws_9.1_distribution_tuning.md",
        "华为云 GaussDB(DWS) 9.1 分布键、存储倾斜与查询调优指南",
        ["DWS", "9.1", "分布键", "table_distribution", "pgxc_thread_wait_status", "调优"],
    ),
    (
        "DWS",
        "dws/03_dws_9.1_sysviews_errors.md",
        "华为云 GaussDB(DWS) 9.1 高频系统视图与典型报错排障手册",
        ["DWS", "9.1", "系统视图", "报错排障", "锁阻塞", "pgxc_stat_activity"],
    ),
    (
        "INCEPTOR",
        "inceptor/01_inceptor_9.x_sql_storage.md",
        "星环 Inceptor 9.3 / 9.4 (TDH 9.x & ArgoDB) SQL 语法与存储引擎指南",
        ["INCEPTOR", "9.3", "9.4", "Quark", "HOLODESK", "TORC", "ORC", "CONNECT BY"],
    ),
    (
        "INCEPTOR",
        "inceptor/02_inceptor_9.x_partition_tuning.md",
        "星环 Inceptor 9.3 / 9.4 分区、分桶、Compaction 与性能调优",
        ["INCEPTOR", "9.3", "9.4", "Compaction", "分桶", "MapJoin", "reducers"],
    ),
    (
        "INCEPTOR",
        "inceptor/03_inceptor_9.x_merge_errors.md",
        "星环 Inceptor 9.3 / 9.4 MERGE INTO 规范与高频报错排障",
        ["INCEPTOR", "9.3", "9.4", "MERGE INTO", "10284", "ParseException", "Thrift"],
    ),
    (
        "PGSQL",
        "pgsql/01_pgsql_19_sql_features.md",
        "PostgreSQL 19 / 18 / 17 新特性与现代 SQL 语法参考",
        ["PGSQL", "19", "18", "17", "REPACK CONCURRENTLY", "ON CONFLICT DO SELECT", "IGNORE NULLS", "uuidv7", "JSON_TABLE"],
    ),
    (
        "PGSQL",
        "pgsql/02_pgsql_19_index_explain_tuning.md",
        "PostgreSQL 19 / 18 / 17 索引、执行计划与性能调优指南",
        ["PGSQL", "19", "18", "17", "Skip Scan", "Async I/O", "EXPLAIN", "VACUUM", "TidStore"],
    ),
    (
        "PGSQL",
        "pgsql/03_pgsql_19_catalogs_sqlstate.md",
        "PostgreSQL 19 / 18 / 17 系统目录排障与 SQLSTATE 错误码手册",
        ["PGSQL", "19", "18", "17", "SQLSTATE", "pg_stat_io", "pg_blocking_pids", "锁阻塞"],
    ),
]

PACK_SPECS: List[Tuple[str, str]] = [
    ("DWS", "DataOG_KB_DWS_9.1.json.gz"),
    ("INCEPTOR", "DataOG_KB_INCEPTOR_9.x.json.gz"),
    ("PGSQL", "DataOG_KB_PGSQL_19.json.gz"),
    ("", "DataOG_KB_ALL_STARTER.json.gz"),
]


def _verify_pack_roundtrip(pack_path: Path, dialect: str, expected_docs: int) -> Dict[str, int]:
    """Import a built .json.gz pack into a fresh PlatformContext and verify round-trip behavior."""
    raw = pack_path.read_bytes()
    payload = json.loads(gzip.decompress(raw).decode("utf-8"))
    if payload.get("format") != "dataog_kb_pack_v1":
        raise RuntimeError(f"Unexpected pack format in {pack_path.name}: {payload.get('format')}")
    if int(payload.get("doc_count", 0)) != expected_docs:
        raise RuntimeError(
            f"Expected {expected_docs} docs in {pack_path.name}, got {payload.get('doc_count')}"
        )

    with tempfile.TemporaryDirectory() as verify_dir:
        verify_ctx = PlatformContext(private_root=verify_dir)
        res1 = verify_ctx.knowledge_hub.import_pack_bytes(raw, overwrite_existing=False)
        if not res1.get("ok") or int(res1.get("imported_count", 0)) != expected_docs:
            raise RuntimeError(f"Round-trip import failed for {pack_path.name}: {res1}")

        # Duplicate import without overwrite must skip all already-present documents
        res2 = verify_ctx.knowledge_hub.import_pack_bytes(raw, overwrite_existing=False)
        if not res2.get("ok") or int(res2.get("skipped_count", 0)) != expected_docs:
            raise RuntimeError(f"Duplicate-skip check failed for {pack_path.name}: {res2}")

        docs = verify_ctx.knowledge_hub.list_documents(dialect=dialect, include_builtin=False)
        if len(docs) != expected_docs:
            raise RuntimeError(
                f"Expected {expected_docs} user docs for dialect={dialect!r}, got {len(docs)}"
            )
        total_chunks = sum(int(d.get("chunk_count", 0)) for d in docs)

        # Verify FTS5 retrieval & strict dialect isolation (without builtin docs)
        if dialect in ("DWS", ""):
            r_dws = verify_ctx.knowledge_hub.search(
                "table_distribution HSTORE_OPT enable_binlog",
                dialect="DWS",
                include_builtin=False,
                use_embeddings=False,
            )
            if not r_dws.get("hits"):
                raise RuntimeError(f"DWS FTS5 retrieval returned no hits in {pack_path.name}")
            r_cross = verify_ctx.knowledge_hub.search(
                "table_distribution",
                dialect="INCEPTOR",
                include_builtin=False,
                use_embeddings=False,
            )
            if any(h.get("dialect") == "DWS" for h in r_cross.get("hits", [])):
                raise RuntimeError(f"Cross-dialect leak detected in {pack_path.name} (DWS leaked into INCEPTOR)")

        if dialect in ("INCEPTOR", ""):
            r_inc = verify_ctx.knowledge_hub.search(
                "SemanticException Error 10284 COMPACT major MERGE INTO",
                dialect="INCEPTOR",
                include_builtin=False,
                use_embeddings=False,
            )
            if not r_inc.get("hits"):
                raise RuntimeError(f"INCEPTOR FTS5 retrieval returned no hits in {pack_path.name}")

        if dialect in ("PGSQL", ""):
            r_pg = verify_ctx.knowledge_hub.search(
                "REPACK CONCURRENTLY ON CONFLICT DO SELECT IGNORE NULLS uuidv7",
                dialect="PGSQL",
                include_builtin=False,
                use_embeddings=False,
            )
            if not r_pg.get("hits"):
                raise RuntimeError(f"PGSQL FTS5 retrieval returned no hits in {pack_path.name}")

        return {
            "docs": len(docs),
            "chunks": total_chunks,
            "bytes": len(raw),
        }


def build_all() -> None:
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as build_dir:
        ctx = PlatformContext(private_root=build_dir)
        ingested_summary: List[Dict[str, object]] = []

        for dialect, rel_path, title, tags in SOURCE_SPECS:
            src_path = SOURCES_DIR / rel_path
            if not src_path.is_file():
                raise FileNotFoundError(f"Missing starter source doc: {src_path}")
            content = src_path.read_bytes()
            result = ctx.knowledge_hub.ingest_uploaded_file(
                filename=src_path.name,
                content=content,
                dialect=dialect,
                title=title,
                tags=tags,
            )
            if not result.get("ok"):
                raise RuntimeError(f"Failed ingesting {rel_path}: {result}")
            doc_obj = result.get("document") if isinstance(result.get("document"), dict) else result
            ingested_summary.append(
                {
                    "dialect": dialect,
                    "file": rel_path,
                    "chunks": int(doc_obj.get("chunk_count", 0)),
                    "source_bytes": len(content),
                }
            )

        print("=== Ingested Starter Markdown Sources ===")
        for item in ingested_summary:
            print(
                f"  [{item['dialect']:<8}] {item['file']:<44} "
                f"chunks={item['chunks']:<3} raw={item['source_bytes']} B"
            )

        print("\n=== Exported & Verified Knowledge Packs ===")
        for dialect, out_name in PACK_SPECS:
            pack_bytes, _ = ctx.knowledge_hub.export_pack_bytes(dialect=dialect)
            out_path = PACKS_DIR / out_name
            out_path.write_bytes(pack_bytes)
            expected_docs = 3 if dialect else len(SOURCE_SPECS)
            stats = _verify_pack_roundtrip(out_path, dialect, expected_docs)
            print(
                f"  {out_name:<34} docs={stats['docs']:<2} chunks={stats['chunks']:<3} "
                f"size={stats['bytes']} B ({stats['bytes'] / 1024:.1f} KB) [FTS5 + round-trip OK]"
            )


if __name__ == "__main__":
    build_all()
