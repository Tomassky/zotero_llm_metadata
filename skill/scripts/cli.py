"""
Command-line interface for read-only Zotero access + knowledge-graph queries.

This replaces the former MCP server. Every subcommand prints a Markdown string
to stdout. The Zotero skill (`.claude/skills/zotero/`) drives it through the
`bin/zot` wrapper.

Two families of subcommands:

  Live Zotero (needs Zotero desktop running, ZOTERO_LOCAL=true):
    search, tag-search, metadata, fulltext, collections, collection-items,
    children, tags, advanced-search, annotations, notes, search-notes, libraries

  Knowledge graph (offline, reads graph/graph.json):
    graph-search, graph-explore, graph-community, graph-bridge, graph-central,
    graph-neighbors, graph-path
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import graph_query, operations


def _configure_logging() -> None:
    # Logs go to stderr; stdout stays clean Markdown for the caller.
    logging.basicConfig(
        level=os.environ.get("ZOTERO_LOG_LEVEL", "WARNING").upper(),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zot",
        description="Read-only Zotero access and knowledge-graph queries.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- Live Zotero ---
    p = sub.add_parser("search", help="Search items by query string")
    p.add_argument("query")
    p.add_argument("--qmode", default="titleCreatorYear",
                   choices=["titleCreatorYear", "everything"])
    p.add_argument("--item-type", default="-attachment")
    p.add_argument("--limit", default=10)
    p.add_argument("--tag", action="append", help="Tag filter (repeatable)")

    p = sub.add_parser("tag-search", help="Search items by tag conditions")
    p.add_argument("tag", nargs="+", help="Tag condition(s); ANDed. Use 'a OR b' / '-c'.")
    p.add_argument("--item-type", default="-attachment")
    p.add_argument("--limit", default=10)

    p = sub.add_parser("metadata", help="Get metadata for an item")
    p.add_argument("item_key")
    p.add_argument("--no-abstract", action="store_true")
    p.add_argument("--format", dest="fmt", default="markdown",
                   choices=["markdown", "bibtex"])

    p = sub.add_parser("fulltext", help="Get full text of an item")
    p.add_argument("item_key")

    p = sub.add_parser("collections", help="List all collections")
    p.add_argument("--limit", default=None)

    p = sub.add_parser("collection-items", help="List items in a collection")
    p.add_argument("collection_key")
    p.add_argument("--limit", default=50)

    p = sub.add_parser("children", help="List child items (attachments/notes) of an item")
    p.add_argument("item_key")

    p = sub.add_parser("tags", help="List all tags")
    p.add_argument("--limit", default=None)

    p = sub.add_parser("advanced-search", help="Advanced multi-condition search")
    p.add_argument("conditions", help='JSON list, e.g. \'[{"field":"tag","operation":"is","value":"llm"}]\'')
    p.add_argument("--join", dest="join_mode", default="all", choices=["all", "any"])
    p.add_argument("--sort-by", default=None)
    p.add_argument("--sort-direction", default="asc", choices=["asc", "desc"])
    p.add_argument("--limit", default=50)

    p = sub.add_parser("annotations", help="Get annotations for an item or library-wide")
    p.add_argument("--item-key", default=None)
    p.add_argument("--pdf", action="store_true", help="Fallback to direct PDF extraction")
    p.add_argument("--limit", default=None)

    p = sub.add_parser("notes", help="Get notes")
    p.add_argument("--item-key", default=None)
    p.add_argument("--limit", default=20)
    p.add_argument("--no-truncate", action="store_true")

    p = sub.add_parser("search-notes", help="Search notes and annotations")
    p.add_argument("query")
    p.add_argument("--limit", default=20)

    sub.add_parser("libraries", help="List accessible libraries")

    # --- Knowledge graph ---
    p = sub.add_parser("graph-search", help="Keyword search over the knowledge graph")
    p.add_argument("query")
    p.add_argument("--top-n", type=int, default=10)

    p = sub.add_parser("graph-explore", help="Full graph context for an item")
    p.add_argument("item_key")
    p.add_argument("--neighbor-limit", type=int, default=10)

    p = sub.add_parser("graph-community", help="List communities or show members of matches")
    p.add_argument("label", nargs="?", default="")
    p.add_argument("--top-n", type=int, default=15)

    p = sub.add_parser("graph-bridge", help="Items linking two topics/communities")
    p.add_argument("topic_a")
    p.add_argument("topic_b")
    p.add_argument("--top-n", type=int, default=10)

    p = sub.add_parser("graph-central", help="Most-connected items (community or whole graph)")
    p.add_argument("--community", default="")
    p.add_argument("--top-n", type=int, default=10)

    p = sub.add_parser("graph-neighbors", help="Neighbors of an item")
    p.add_argument("item_key")
    p.add_argument("--by-community", action="store_true")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("graph-path", help="Shortest path between two items")
    p.add_argument("key_a")
    p.add_argument("key_b")

    return parser


def _dispatch(args: argparse.Namespace) -> str:
    cmd = args.command

    # Live Zotero
    if cmd == "search":
        return operations.search_items(args.query, args.qmode, args.item_type, args.limit, args.tag)
    if cmd == "tag-search":
        return operations.search_by_tag(args.tag, args.item_type, args.limit)
    if cmd == "metadata":
        return operations.get_item_metadata(args.item_key, not args.no_abstract, args.fmt)
    if cmd == "fulltext":
        return operations.get_item_fulltext(args.item_key)
    if cmd == "collections":
        return operations.get_collections(args.limit)
    if cmd == "collection-items":
        return operations.get_collection_items(args.collection_key, args.limit)
    if cmd == "children":
        return operations.get_item_children(args.item_key)
    if cmd == "tags":
        return operations.get_tags(args.limit)
    if cmd == "advanced-search":
        return operations.advanced_search(
            args.conditions, args.join_mode, args.sort_by, args.sort_direction, args.limit
        )
    if cmd == "annotations":
        return operations.get_annotations(args.item_key, args.pdf, args.limit)
    if cmd == "notes":
        return operations.get_notes(args.item_key, args.limit, not args.no_truncate)
    if cmd == "search-notes":
        return operations.search_notes(args.query, args.limit)
    if cmd == "libraries":
        return operations.list_libraries()

    # Knowledge graph
    if cmd == "graph-search":
        return graph_query.graph_search(args.query, args.top_n)
    if cmd == "graph-explore":
        return graph_query.graph_explore_item(args.item_key, args.neighbor_limit)
    if cmd == "graph-community":
        return graph_query.graph_community_info(args.label, args.top_n)
    if cmd == "graph-bridge":
        return graph_query.graph_bridge(args.topic_a, args.topic_b, args.top_n)
    if cmd == "graph-central":
        return graph_query.graph_central(args.community, args.top_n)
    if cmd == "graph-neighbors":
        return graph_query.graph_neighbors(args.item_key, args.by_community, args.limit)
    if cmd == "graph-path":
        return graph_query.graph_path(args.key_a, args.key_b)

    return f"Unknown command: {cmd}"


def main(argv: list[str] | None = None) -> int:
    # Default to local Zotero mode unless the caller configured web mode.
    os.environ.setdefault("ZOTERO_LOCAL", "true")
    _configure_logging()

    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        print(_dispatch(args))
        return 0
    except BrokenPipeError:
        return 0
    except Exception as e:  # last-resort guard; operations already catch their own
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
