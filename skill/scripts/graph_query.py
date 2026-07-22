"""
Knowledge-graph queries over a pre-built ``graph/graph.json``.

Replaces the former ``graph_search.py`` MCP tools. Reads the JSON directly and
fixes the query-layer problems that made the old tools ineffective:

  1. Every result row now carries the Zotero **item key**, so search → explore
     can be chained (the old output printed only titles).
  2. The query is **synonym-normalized** the same way tags were at build time
     (via the vendored ``synonyms.normalize_tag`` / ``resolve_synonym``), so
     English or variant queries such as ``LLM`` / ``RAG`` hit the canonical
     Chinese tags.
  3. Community-label matching **collects every match** instead of breaking on the
     first one (``大语言模型`` lives in 3 communities).

Plus new query shapes that keyword search cannot express: ``bridge`` (items
linking two topics), ``central`` (most-connected items), ``neighbors`` (grouped
by community) and ``path`` (shortest path between two items).

No Zotero connection required — everything is offline against the JSON.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_graph_cache: dict | None = None


# ---------------------------------------------------------------------------
# Graph location + loading
# ---------------------------------------------------------------------------

def _resolve_graph_json() -> Path:
    """Locate ``graph/graph.json`` by walking up from this file.

    Honors ``ZOTERO_GRAPH_JSON`` if set. Otherwise searches ancestor directories
    for a ``graph/graph.json`` — robust regardless of how deep this skill is
    nested (it lives at ``<repo>/skill/scripts`` and, when installed, at
    ``<repo>/.claude/skills/zotero/scripts``). Both walk up to the repo-root
    ``graph/graph.json`` produced by ``--build-graph``.
    """
    env = os.environ.get("ZOTERO_GRAPH_JSON")
    if env:
        return Path(env)
    cur = Path(__file__).resolve().parent
    for _ in range(12):
        cand = cur / "graph" / "graph.json"
        if cand.exists():
            return cand
        cur = cur.parent
    # Last resort: relative to the current working directory (docs say run from
    # the repo root). If it doesn't exist, _ensure_graph reports a clear error.
    return Path("graph") / "graph.json"


def _load_graph_data() -> dict[str, Any]:
    global _graph_cache
    if _graph_cache is None:
        with open(_resolve_graph_json(), encoding="utf-8") as f:
            _graph_cache = json.load(f)
    assert _graph_cache is not None
    return _graph_cache


def _ensure_graph() -> str | None:
    if not _resolve_graph_json().exists():
        return "No graph found. Run `python -m zotero_llm_metadata --build-graph` in zotero_llm_metadata first."
    return None


# ---------------------------------------------------------------------------
# Synonym-normalized query variants (vendored synonyms — no repo import)
# ---------------------------------------------------------------------------

def _query_variants(query: str) -> list[str]:
    """Return the lowercased raw query plus its synonym-normalized canonical form.

    Both are used for substring matching so English/variant queries reach the
    Chinese canonical tags stored in the graph.
    """
    variants = {query.strip().lower()}
    try:
        # ``synonyms`` is a sibling module in this skill's scripts/ dir.
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from synonyms import normalize_tag, resolve_synonym

        variants.add(resolve_synonym(normalize_tag(query)))
    except Exception as e:  # pragma: no cover - defensive fallback
        logger.debug("synonym normalization unavailable: %s", e)
    return [v for v in variants if v]


def _node_matches(node: dict, variants: list[str]) -> bool:
    label = node.get("label", "").lower()
    tags = " ".join(node.get("tags_normalized", [])).lower()
    abstract = node.get("abstract", "").lower()
    return any(v in label or v in tags or v in abstract for v in variants)


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

def _build_edge_index(data: dict) -> dict[str, list[dict]]:
    idx: dict[str, list[dict]] = {}
    for edge in data.get("edges", []):
        src, tgt = edge.get("source", ""), edge.get("target", "")
        if src:
            idx.setdefault(src, []).append(edge)
        if tgt:
            idx.setdefault(tgt, []).append(edge)
    return idx


def _build_nodes_map(data: dict) -> dict[str, dict]:
    return {n["id"]: n for n in data.get("nodes", []) if "id" in n}


def _neighbor_id(edge: dict, node_id: str) -> str:
    return edge.get("target", "") if edge.get("source") == node_id else edge.get("source", "")


def _format_neighbors(
    node_id: str,
    edge_index: dict[str, list[dict]],
    nodes_map: dict[str, dict],
    limit: int = 8,
) -> list[str]:
    edges = sorted(edge_index.get(node_id, []), key=lambda e: e.get("weight", 1), reverse=True)
    lines = []
    for edge in edges[:limit]:
        nid = _neighbor_id(edge, node_id)
        neighbor = nodes_map.get(nid)
        label = neighbor.get("label", nid) if neighbor else nid
        rel = edge.get("relation", "")
        tags = ", ".join(edge.get("shared_tags", [])[:3])
        weight = edge.get("weight", 1)
        lines.append(f"- `{label}` [{nid}] ({rel}, weight={weight}, tags: {tags})")
    return lines


def _matched_communities(data: dict, variants: list[str]) -> list[tuple[str, dict]]:
    """All communities whose label matches any query variant (fixes break-bug)."""
    matched = []
    for cid_str, comm in data.get("communities", {}).items():
        label = comm.get("label", "").lower()
        if any(v in label or label in v for v in variants):
            matched.append((cid_str, comm))
    return matched


# ---------------------------------------------------------------------------
# Tool 1: search
# ---------------------------------------------------------------------------

def graph_search(query: str, top_n: int = 10) -> str:
    """Keyword search across titles, tags and community labels.

    Returns matched items (each with its **item key**, community and top
    neighbors) plus every community whose label matches the query.
    """
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)
        variants = _query_variants(query)

        output: list[str] = []

        matched_communities = _matched_communities(data, variants)
        comm_member_ids: set[str] = set()
        for _, comm in matched_communities:
            comm_member_ids.update(comm.get("members", []))

        matched_nodes = [n for n in data.get("nodes", []) if _node_matches(n, variants)]
        # Items already shown under a matched community are listed there.
        outside_nodes = [n for n in matched_nodes if n.get("id") not in comm_member_ids]

        if variants:
            output.append(f"# Graph search: \"{query}\"  (matched forms: {', '.join(variants)})")
            output.append("")

        for cid_str, comm in matched_communities:
            label = comm.get("label", f"Community {cid_str}")
            size = comm.get("size", len(comm.get("members", [])))
            cohesion = comm.get("cohesion", 0)
            output.append(f"## Community: {label}  (#{cid_str})")
            output.append(f"**{size} items · cohesion: {cohesion}**")
            for mid in comm.get("members", [])[:top_n]:
                m_node = nodes_map.get(mid)
                if m_node:
                    degree = len(edge_index.get(mid, []))
                    output.append(f"- `{m_node.get('label', mid)}` [{mid}] ({degree} connections)")
            if len(comm.get("members", [])) > top_n:
                output.append(f"... and {len(comm['members']) - top_n} more")
            output.append("")

        if outside_nodes:
            heading = "## Also matched (outside those communities)" if matched_communities else "## Matched items"
            output.append(heading)
            output.append(f"Found {len(outside_nodes)} items")
            output.append("")
            for node in outside_nodes[:top_n]:
                nid = node.get("id", "")
                degree = len(edge_index.get(nid, []))
                output.append(
                    f"- **{node.get('label', nid)}** [{nid}] — "
                    f"community: {node.get('community_label', 'Unknown')}, {degree} connections"
                )
                for nb in _format_neighbors(nid, edge_index, nodes_map, limit=3):
                    output.append(f"  {nb}")
            output.append("")

        if not matched_communities and not outside_nodes:
            output.append(f"No matches for \"{query}\" in the knowledge graph.")
            output.append("")
            output.append("Available communities:")
            for cid_str, comm in data.get("communities", {}).items():
                output.append(f"- {comm.get('label', cid_str)} ({comm.get('size', 0)} items)")

        return "\n".join(output).rstrip()

    except Exception as e:
        logger.error("Error searching graph: %s", e)
        return f"Error searching graph: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 2: explore item
# ---------------------------------------------------------------------------

def graph_explore_item(item_key: str, neighbor_limit: int = 10) -> str:
    """Full graph context for a single item: community, tags, neighbors."""
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)

        node = nodes_map.get(item_key)
        if not node:
            return f"Item key '{item_key}' not found in graph."

        tags = ", ".join(node.get("tags_normalized", [])[:8])
        degree = len(edge_index.get(item_key, []))

        output = [
            f"# {node.get('label', item_key)}  [{item_key}]",
            f"**Community:** {node.get('community_label', 'Unknown')} (#{node.get('community')})",
            f"**Tags:** {tags}",
            f"**Connections:** {degree}",
            "",
        ]
        neighbors = _format_neighbors(item_key, edge_index, nodes_map, limit=neighbor_limit)
        if neighbors:
            output.append("## Neighbors (by connection strength)")
            output.extend(neighbors)
        return "\n".join(output)

    except Exception as e:
        logger.error("Error exploring item: %s", e)
        return f"Error exploring item: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 3: community info
# ---------------------------------------------------------------------------

def graph_community_info(community_label: str = "", top_n: int = 15) -> str:
    """List all communities, or show members of every community matching a label."""
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)

        if not community_label:
            output = ["# Knowledge Graph Communities"]
            for cid_str, comm in data.get("communities", {}).items():
                output.append(
                    f"- **{comm.get('label', cid_str)}** (#{cid_str}, "
                    f"{comm.get('size', 0)} items, cohesion: {comm.get('cohesion', 0)})"
                )
            stats = data.get("stats", {})
            validation = data.get("validation", {})
            output.append("")
            output.append(
                f"Total: {stats.get('nodes', 0)} items · {stats.get('edges', 0)} edges "
                f"· {stats.get('avg_degree', 0)} avg degree · Modularity: {validation.get('modularity', 0)}"
            )
            return "\n".join(output)

        variants = _query_variants(community_label)
        matched = _matched_communities(data, variants)
        if not matched:
            output = [f"Community \"{community_label}\" not found.", "", "Available communities:"]
            for cid_str, comm in data.get("communities", {}).items():
                output.append(f"- {comm.get('label', cid_str)} ({comm.get('size', 0)} items)")
            return "\n".join(output)

        output = []
        for cid_str, comm in matched:
            label = comm.get("label", f"Community {cid_str}")
            members = comm.get("members", [])
            output.append(f"# Community: {label}  (#{cid_str})")
            output.append(f"**{comm.get('size', len(members))} items · cohesion: {comm.get('cohesion', 0)}**")
            output.append("")
            output.append("## Members")
            for mid in members[:top_n]:
                m_node = nodes_map.get(mid)
                if m_node:
                    degree = len(edge_index.get(mid, []))
                    m_tags = ", ".join(m_node.get("tags_normalized", [])[:3])
                    output.append(f"- `{m_node.get('label', mid)}` [{mid}] ({degree} connections, tags: {m_tags})")
            if len(members) > top_n:
                output.append(f"... and {len(members) - top_n} more")
            output.append("")

        return "\n".join(output).rstrip()

    except Exception as e:
        logger.error("Error getting community info: %s", e)
        return f"Error getting community info: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 4: bridge — items linking two topics
# ---------------------------------------------------------------------------

def graph_bridge(topic_a: str, topic_b: str, top_n: int = 10) -> str:
    """Find items/edges that connect two topics (or communities).

    Resolves each topic to a set of nodes (by tag/title/community match), then
    reports (a) direct edges spanning the two sets and (b) connector items that
    neighbor both sets.
    """
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)

        def resolve_set(topic: str) -> set[str]:
            variants = _query_variants(topic)
            ids = {n["id"] for n in data.get("nodes", []) if _node_matches(n, variants)}
            for _, comm in _matched_communities(data, variants):
                ids.update(comm.get("members", []))
            return ids

        set_a, set_b = resolve_set(topic_a), resolve_set(topic_b)
        if not set_a:
            return f"No graph nodes match topic \"{topic_a}\"."
        if not set_b:
            return f"No graph nodes match topic \"{topic_b}\"."

        output = [f"# Bridges: \"{topic_a}\" ↔ \"{topic_b}\"",
                  f"({len(set_a)} nodes vs {len(set_b)} nodes)", ""]

        # (a) Direct edges spanning the two sets.
        direct = []
        seen_pairs: set[tuple[str, str]] = set()
        for edge in data.get("edges", []):
            s, t = edge.get("source", ""), edge.get("target", "")
            if (s in set_a and t in set_b) or (s in set_b and t in set_a):
                pair = tuple(sorted((s, t)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                direct.append(edge)
        direct.sort(key=lambda e: e.get("weight", 1), reverse=True)

        output.append(f"## Direct connections ({len(direct)})")
        if direct:
            for edge in direct[:top_n]:
                s, t = edge.get("source", ""), edge.get("target", "")
                sl = nodes_map.get(s, {}).get("label", s)
                tl = nodes_map.get(t, {}).get("label", t)
                tags = ", ".join(edge.get("shared_tags", [])[:3])
                output.append(f"- `{sl}` [{s}] ↔ `{tl}` [{t}] "
                              f"({edge.get('relation', '')}, weight={edge.get('weight', 1)}, tags: {tags})")
        else:
            output.append("- None (the two topics are not directly linked)")
        output.append("")

        # (b) Connector nodes: neighbor both sets, belong to neither.
        connectors = []
        for nid, edges in edge_index.items():
            if nid in set_a or nid in set_b:
                continue
            neigh = {_neighbor_id(e, nid) for e in edges}
            if neigh & set_a and neigh & set_b:
                connectors.append((nid, len(edges)))
        connectors.sort(key=lambda x: x[1], reverse=True)

        output.append(f"## Connector items neighboring both ({len(connectors)})")
        if connectors:
            for nid, degree in connectors[:top_n]:
                node = nodes_map.get(nid, {})
                output.append(f"- `{node.get('label', nid)}` [{nid}] — "
                              f"community: {node.get('community_label', '?')}, {degree} connections")
        else:
            output.append("- None")

        return "\n".join(output)

    except Exception as e:
        logger.error("Error finding bridges: %s", e)
        return f"Error finding bridges: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 5: central — most-connected items
# ---------------------------------------------------------------------------

def graph_central(community_label: str = "", top_n: int = 10) -> str:
    """Most-connected items (god nodes) for a community, or the whole graph."""
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)

        if community_label:
            matched = _matched_communities(data, _query_variants(community_label))
            if not matched:
                return f"Community \"{community_label}\" not found."
            scope_ids: set[str] = set()
            labels = []
            for cid_str, comm in matched:
                scope_ids.update(comm.get("members", []))
                labels.append(f"{comm.get('label', cid_str)} (#{cid_str})")
            title = f"# Central items in: {', '.join(labels)}"
        else:
            scope_ids = set(nodes_map.keys())
            title = "# Central items (whole graph)"

        ranked = sorted(
            ((nid, len(edge_index.get(nid, []))) for nid in scope_ids),
            key=lambda x: x[1],
            reverse=True,
        )
        output = [title, ""]
        for i, (nid, degree) in enumerate(ranked[:top_n], 1):
            if degree == 0:
                continue
            node = nodes_map.get(nid, {})
            tags = ", ".join(node.get("tags_normalized", [])[:4])
            output.append(f"{i}. `{node.get('label', nid)}` [{nid}] — {degree} connections "
                          f"(community: {node.get('community_label', '?')}, tags: {tags})")
        return "\n".join(output)

    except Exception as e:
        logger.error("Error computing central items: %s", e)
        return f"Error computing central items: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 6: neighbors — grouped by community
# ---------------------------------------------------------------------------

def graph_neighbors(item_key: str, by_community: bool = False, limit: int = 20) -> str:
    """Neighbors of an item, optionally grouped by community."""
    err = _ensure_graph()
    if err:
        return err
    try:
        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        edge_index = _build_edge_index(data)

        if item_key not in nodes_map:
            return f"Item key '{item_key}' not found in graph."

        edges = sorted(edge_index.get(item_key, []), key=lambda e: e.get("weight", 1), reverse=True)
        if not edges:
            return f"`{nodes_map[item_key].get('label', item_key)}` [{item_key}] has no neighbors."

        header = f"# Neighbors of `{nodes_map[item_key].get('label', item_key)}` [{item_key}]"
        if not by_community:
            output = [header, ""]
            for edge in edges[:limit]:
                nid = _neighbor_id(edge, item_key)
                node = nodes_map.get(nid, {})
                tags = ", ".join(edge.get("shared_tags", [])[:3])
                output.append(f"- `{node.get('label', nid)}` [{nid}] "
                              f"({edge.get('relation', '')}, weight={edge.get('weight', 1)}, tags: {tags})")
            return "\n".join(output)

        groups: dict[str, list[str]] = {}
        for edge in edges[:limit]:
            nid = _neighbor_id(edge, item_key)
            node = nodes_map.get(nid, {})
            clabel = node.get("community_label", "Unknown")
            groups.setdefault(clabel, []).append(
                f"- `{node.get('label', nid)}` [{nid}] "
                f"({edge.get('relation', '')}, weight={edge.get('weight', 1)})"
            )
        output = [header, ""]
        for clabel, lines in sorted(groups.items(), key=lambda x: len(x[1]), reverse=True):
            output.append(f"## {clabel} ({len(lines)})")
            output.extend(lines)
            output.append("")
        return "\n".join(output).rstrip()

    except Exception as e:
        logger.error("Error listing neighbors: %s", e)
        return f"Error listing neighbors: {str(e)}"


# ---------------------------------------------------------------------------
# Tool 7: path — shortest path between two items
# ---------------------------------------------------------------------------

def graph_path(key_a: str, key_b: str) -> str:
    """Shortest path between two items through the graph."""
    err = _ensure_graph()
    if err:
        return err
    try:
        import networkx as nx

        data = _load_graph_data()
        nodes_map = _build_nodes_map(data)
        if key_a not in nodes_map:
            return f"Item key '{key_a}' not found in graph."
        if key_b not in nodes_map:
            return f"Item key '{key_b}' not found in graph."

        G = nx.Graph()
        for edge in data.get("edges", []):
            s, t = edge.get("source", ""), edge.get("target", "")
            if s and t:
                G.add_edge(s, t, **edge)

        if key_a not in G or key_b not in G or not nx.has_path(G, key_a, key_b):
            return (f"No path between `{nodes_map[key_a].get('label', key_a)}` and "
                    f"`{nodes_map[key_b].get('label', key_b)}` (disconnected).")

        path = nx.shortest_path(G, key_a, key_b)
        output = [f"# Path: `{nodes_map[key_a].get('label', key_a)}` → "
                  f"`{nodes_map[key_b].get('label', key_b)}`",
                  f"**{len(path) - 1} hops**", ""]
        for i, nid in enumerate(path):
            node = nodes_map.get(nid, {})
            prefix = f"{i}." if i == 0 else f"{i}. ↓"
            output.append(f"{prefix} `{node.get('label', nid)}` [{nid}] "
                          f"— {node.get('community_label', '?')}")
            if i < len(path) - 1:
                edge = G.get_edge_data(nid, path[i + 1]) or {}
                tags = ", ".join(edge.get("shared_tags", [])[:3])
                output.append(f"     via {edge.get('relation', '')} (weight={edge.get('weight', 1)}, tags: {tags})")
        return "\n".join(output)

    except Exception as e:
        logger.error("Error finding path: %s", e)
        return f"Error finding path: {str(e)}"
