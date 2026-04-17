"""
Build a Zotero knowledge graph from the local SQLite database.

Reads items, tags, and collections from zotero.sqlite,
constructs a NetworkX graph with same_collection and shared_tag edges,
runs Leiden/Louvain community detection, and exports results.

Usage:
    python3 __main__.py --build-graph
    python3 __main__.py --build-graph --max-degree 15
    python3 __main__.py --build-graph --tag-fraction 0.25
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data loading from Zotero SQLite
# ---------------------------------------------------------------------------

def fetch_items_from_db(db_path: str) -> list[dict[str, Any]]:
    """Fetch all Zotero items with their tags and collections from the local DB.

    Returns a list of item dicts suitable for graph_builder.build_graph().
    """
    uri = f"file:{db_path}?immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    # 1. Get all non-attachment/non-note items
    items_query = """
    SELECT
        i.itemID,
        i.key,
        it.typeName as item_type,
        title_val.value as title,
        abstract_val.value as abstract
    FROM items i
    JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
    LEFT JOIN itemData id_title ON i.itemID = id_title.itemID
        AND id_title.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'title')
    LEFT JOIN itemDataValues title_val ON id_title.valueID = title_val.valueID
    LEFT JOIN itemData id_abs ON i.itemID = id_abs.itemID
        AND id_abs.fieldID = (SELECT fieldID FROM fields WHERE fieldName = 'abstractNote')
    LEFT JOIN itemDataValues abstract_val ON id_abs.valueID = abstract_val.valueID
    WHERE it.typeName NOT IN ('attachment', 'note', 'annotation')
    ORDER BY i.key
    """
    items_rows = conn.execute(items_query).fetchall()

    # 2. Get all tags per item
    tags_query = """
    SELECT it.itemID, t.name as tag_name
    FROM itemTags it
    JOIN tags t ON it.tagID = t.tagID
    """
    tags_rows = conn.execute(tags_query).fetchall()

    # Build tag lookup: itemID -> [tag_name]
    tags_by_item: dict[int, list[str]] = {}
    for row in tags_rows:
        tags_by_item.setdefault(row["itemID"], []).append(row["tag_name"])

    # 3. Get all collections per item
    collections_query = """
    SELECT ci.itemID, c.collectionName, c.key as collection_key
    FROM collectionItems ci
    JOIN collections c ON ci.collectionID = c.collectionID
    """
    collections_rows = conn.execute(collections_query).fetchall()

    # Build collection lookup: itemID -> [collection_key]
    # Note: we use collection_key (short hash) like Zotero API does
    collections_by_item: dict[int, list[str]] = {}
    collection_names: dict[str, str] = {}  # collection_key -> name
    for row in collections_rows:
        collections_by_item.setdefault(row["itemID"], []).append(row["collection_key"])
        collection_names[row["collection_key"]] = row["collectionName"]

    # 4. Assemble items
    result = []
    for row in items_rows:
        item_id = row["itemID"]
        key = row["key"]
        tags = tags_by_item.get(item_id, [])
        coll_keys = collections_by_item.get(item_id, [])

        # Format tags as Zotero API format [{"tag": "..."}]
        tags_api = [{"tag": t} for t in tags]

        result.append({
            "key": key,
            "title": row["title"] or key,
            "abstract": row["abstract"] or "",
            "itemType": row["item_type"],
            "tags": tags_api,
            "collections": coll_keys,
        })

    conn.close()

    # Store collection name mapping for later use
    # (attached as metadata on the function call result)
    return result, collection_names


# ---------------------------------------------------------------------------
# Tag normalization (reuses graph_builder logic)
# ---------------------------------------------------------------------------

def normalize_tag(tag: str) -> str:
    """Normalize a tag for deduplication."""
    import re
    t = tag.strip().lower()
    if t.startswith("#"):
        t = t[1:]
    t = re.sub(r"[/\-_]+", "-", t)
    t = re.sub(r"\s+", "-", t)
    return t.strip("-")


class TagFilter:
    """Filter tags by granularity."""

    def __init__(self, max_fraction: float = 0.30, min_items: int = 2):
        self.max_fraction = max_fraction
        self.min_items = min_items

    def filter_tags(self, tag_counts: Counter, total_items: int) -> set[str]:
        max_items = max(int(total_items * self.max_fraction), self.min_items + 1)
        valid = set()
        for tag, count in tag_counts.items():
            if count < self.min_items:
                continue
            if count > max_items:
                logger.info(f"Filtered broad tag: '{tag}' ({count}/{total_items} items)")
                continue
            valid.add(tag)
        return valid


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(
    items: list[dict[str, Any]],
    collection_names: dict[str, str] | None = None,
    *,
    max_degree: int = 20,
    tag_filter: TagFilter | None = None,
    collection_weight: float = 2.0,
    tag_weight: float = 1.0,
) -> nx.Graph:
    """Build a NetworkX graph from Zotero items.

    Nodes are Zotero items (keyed by item_key).
    Edges: same_collection (strong), shared_tag (medium).
    """
    if tag_filter is None:
        tag_filter = TagFilter()

    G = nx.Graph()

    # --- Add nodes ---
    item_keys: set[str] = set()
    for item in items:
        key = item.get("key", "")
        if not key:
            continue
        title = item.get("title", "") or key
        item_type = item.get("itemType", "unknown")
        tags_raw = [t.get("tag", "") for t in item.get("tags", []) if isinstance(t, dict)]
        tags_normalized = [normalize_tag(t) for t in tags_raw if t.strip()]
        collections = item.get("collections", [])

        G.add_node(
            key,
            label=title,
            item_type=item_type,
            tags_raw=tags_raw,
            tags_normalized=tags_normalized,
            collections=collections,
            source_file="",
        )
        item_keys.add(key)

    if G.number_of_nodes() == 0:
        return G

    # --- same_collection edges ---
    collection_groups: dict[str, list[str]] = {}
    for key in item_keys:
        for coll_key in G.nodes[key].get("collections", []):
            collection_groups.setdefault(coll_key, []).append(key)

    for coll_key, members in collection_groups.items():
        if len(members) < 2:
            continue
        coll_name = (collection_names or {}).get(coll_key, coll_key)
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                u, v = members[i], members[j]
                if G.has_edge(u, v):
                    G.edges[u, v]["weight"] += collection_weight
                    G.edges[u, v]["relations"].add("same_collection")
                    G.edges[u, v]["collection_name"] = coll_name
                else:
                    G.add_edge(
                        u, v,
                        weight=collection_weight,
                        relations={"same_collection"},
                        shared_tags=set(),
                        collection_name=coll_name,
                        confidence="EXTRACTED",
                    )

    # --- shared_tag edges ---
    tag_counts = Counter()
    for key in item_keys:
        for tag in G.nodes[key].get("tags_normalized", []):
            tag_counts[tag] += 1

    valid_tags = tag_filter.filter_tags(tag_counts, len(item_keys))

    tag_groups: dict[str, list[str]] = {}
    for key in item_keys:
        for tag in G.nodes[key].get("tags_normalized", []):
            if tag in valid_tags:
                tag_groups.setdefault(tag, []).append(key)

    for tag, members in tag_groups.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                u, v = members[i], members[j]
                if G.has_edge(u, v):
                    G.edges[u, v]["weight"] += tag_weight
                    G.edges[u, v]["relations"].add("shared_tag")
                    G.edges[u, v].setdefault("shared_tags", set()).add(tag)
                else:
                    G.add_edge(
                        u, v,
                        weight=tag_weight,
                        relations={"shared_tag"},
                        shared_tags={tag},
                        confidence="EXTRACTED",
                    )

    # --- Clean up edge attrs for JSON ---
    for u, v, data in G.edges(data=True):
        data["relations"] = sorted(data.get("relations", set()))
        data["shared_tags"] = sorted(data.get("shared_tags", set()))
        if "same_collection" in data["relations"]:
            data["relation"] = "same_collection"
        else:
            data["relation"] = "shared_tag"

    # --- Degree cap ---
    for node in list(G.nodes()):
        if G.degree(node) > max_degree:
            edges = sorted(
                G.edges(node, data=True),
                key=lambda e: (-e[2].get("weight", 1.0), -len(e[2].get("shared_tags", []))),
            )
            for u, v, _ in edges[max_degree:]:
                G.remove_edge(u, v)

    logger.info(
        f"Built graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
        f"{len(valid_tags)} valid tags, {len(collection_groups)} collections"
    )
    return G


# ---------------------------------------------------------------------------
# Graph statistics
# ---------------------------------------------------------------------------

def graph_stats(G: nx.Graph) -> dict[str, Any]:
    """Compute structural statistics for graph validation."""
    if G.number_of_nodes() == 0:
        return {"nodes": 0, "edges": 0, "avg_degree": 0, "status": "empty"}

    degrees = [d for _, d in G.degree()]
    isolated = [n for n in G.nodes() if G.degree(n) == 0]
    avg_deg = sum(degrees) / len(degrees) if degrees else 0

    status = "healthy"
    if avg_deg > 20:
        status = "too_dense"
    elif avg_deg < 2:
        status = "too_sparse"
    elif len(isolated) / G.number_of_nodes() > 0.3:
        status = "too_fragmented"

    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "avg_degree": round(avg_deg, 1),
        "max_degree": max(degrees) if degrees else 0,
        "isolated_nodes": len(isolated),
        "isolated_pct": round(len(isolated) / G.number_of_nodes() * 100, 1),
        "status": status,
    }


# ---------------------------------------------------------------------------
# Community detection (Leiden/Louvain)
# ---------------------------------------------------------------------------

def _partition(G: nx.Graph) -> dict[str, int]:
    """Leiden (graspologic) or Louvain (networkx) fallback."""
    import inspect

    try:
        from graspologic.partition import leiden
        import contextlib, io
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()):
                result = leiden(G)
        finally:
            sys.stderr = old_stderr
        return result
    except ImportError:
        logger.info("graspologic not installed, using Louvain")
    except Exception as e:
        logger.warning(f"Leiden failed ({e}), using Louvain")

    kwargs = {"seed": 42, "threshold": 1e-4}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(G, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


def cluster(G: nx.Graph) -> dict[int, list[str]]:
    """Run community detection. Returns {community_id: [node_keys]}.

    Isolated nodes (degree=0) are assigned to the community that
    shares their primary collection, rather than forming single-node
    communities. This reduces fragmentation.
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(G.nodes))}

    isolates = [n for n in G.nodes() if G.degree(n) == 0]
    connected = G.subgraph([n for n in G.nodes() if G.degree(n) > 0])

    raw: dict[int, list[str]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition(connected)
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    # Assign isolated nodes to communities based on shared collections
    # Find collection-majority per community
    community_collections: dict[int, Counter] = {}
    for cid, nodes in raw.items():
        counter = Counter()
        for node in nodes:
            for coll in G.nodes[node].get("collections", []):
                counter[coll] += 1
        community_collections[cid] = counter

    # Find which community each collection is most associated with
    collection_to_cid: dict[str, int] = {}
    for cid, counter in community_collections.items():
        for coll, _ in counter.items():
            prev_cid = collection_to_cid.get(coll)
            if prev_cid is None or counter[coll] > community_collections[prev_cid].get(coll, 0):
                collection_to_cid[coll] = cid

    # Assign each isolate to the community of its first collection
    unassigned = []
    for node in isolates:
        colls = G.nodes[node].get("collections", [])
        assigned = False
        for coll in colls:
            if coll in collection_to_cid:
                raw[collection_to_cid[coll]].append(node)
                assigned = True
                break
        if not assigned:
            unassigned.append(node)

    # Remaining isolates with no collection match → group by shared collection
    if unassigned:
        isolate_coll_groups: dict[str, list[str]] = {}
        for node in unassigned:
            for coll in G.nodes[node].get("collections", []):
                isolate_coll_groups.setdefault(coll, []).append(node)
            if not G.nodes[node].get("collections", []):
                isolate_coll_groups.setdefault("__no_collection__", []).append(node)

        next_cid = max(raw.keys(), default=-1) + 1
        for coll, nodes in isolate_coll_groups.items():
            raw[next_cid] = nodes
            next_cid += 1

    # Split oversized (>25% of nodes)
    max_size = max(10, int(G.number_of_nodes() * 0.25))
    final = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final.extend(_split_community(G, nodes))
        else:
            final.append(nodes)

    final.sort(key=len, reverse=True)
    return {i: sorted(nodes) for i, nodes in enumerate(final)}


def _split_community(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        return [[n] for n in sorted(nodes)]
    try:
        partition = _partition(subgraph)
        sub_comms: dict[int, list[str]] = {}
        for node, cid in partition.items():
            sub_comms.setdefault(cid, []).append(node)
        if len(sub_comms) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_comms.values()]
    except Exception:
        return [sorted(nodes)]


# ---------------------------------------------------------------------------
# Cohesion & validation
# ---------------------------------------------------------------------------

def cohesion_score(G: nx.Graph, community_nodes: list[str]) -> float:
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return round(actual / possible, 2) if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, float]:
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def label_communities(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, str]:
    labels = {}
    for cid, members in communities.items():
        tag_counts: dict[str, int] = {}
        for node in members:
            if node not in G.nodes:
                continue
            for tag in G.nodes[node].get("tags_normalized", []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        if not tag_counts:
            labels[cid] = f"Community {cid}"
            continue
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:2]
        labels[cid] = " / ".join(t for t, _ in top_tags)
    return labels


def validate_communities(G: nx.Graph, communities: dict[int, list[str]]) -> dict[str, Any]:
    """Validate community structure quality."""
    if not communities or G.number_of_edges() == 0:
        return {"modularity": 0, "status": "empty", "issues": ["No communities or edges"]}

    partition = [set(nodes) for nodes in communities.values()]
    mod = round(nx.community.modularity(G, partition), 3)

    sizes = [len(nodes) for nodes in communities.values()]
    total = G.number_of_nodes()
    max_pct = round(max(sizes) / total * 100, 1) if total else 0
    isolated = len([n for n in G.nodes() if G.degree(n) == 0])
    iso_pct = round(isolated / total * 100, 1) if total else 0
    cohesion = score_all(G, communities)
    avg_coh = round(sum(cohesion.values()) / len(cohesion), 2) if cohesion else 0

    issues = []
    if mod < 0.1:
        issues.append("Low modularity (< 0.1) — no meaningful community structure")
    if max_pct > 80:
        issues.append(f"Giant community covers {max_pct}% of nodes")
    if iso_pct > 30:
        issues.append(f"{iso_pct}% isolated nodes")
    if avg_coh < 0.1:
        issues.append("Low avg cohesion — communities loosely connected")

    return {
        "modularity": mod,
        "max_community_pct": max_pct,
        "avg_cohesion": avg_coh,
        "isolated_pct": iso_pct,
        "community_count": len(communities),
        "community_sizes": sizes,
        "issues": issues,
        "status": "healthy" if not issues else "needs_attention",
    }


# ---------------------------------------------------------------------------
# Analysis: god nodes, surprising connections
# ---------------------------------------------------------------------------

def god_nodes(G: nx.Graph, top_n: int = 10) -> list[dict[str, Any]]:
    """Find most connected items."""
    degree = dict(G.degree())
    sorted_nodes = sorted(degree.items(), key=lambda x: x[1], reverse=True)
    result = []
    for node_id, deg in sorted_nodes:
        if deg == 0:
            continue
        result.append({
            "key": node_id,
            "label": G.nodes[node_id].get("label", node_id),
            "edges": deg,
            "tags": G.nodes[node_id].get("tags_normalized", []),
        })
        if len(result) >= top_n:
            break
    return result


def surprising_connections(
    G: nx.Graph,
    communities: dict[int, list[str]],
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Find cross-community edges that reveal unexpected connections."""
    if not communities or G.number_of_edges() == 0:
        return []

    node_community = {n: cid for cid, nodes in communities.items() for n in nodes}
    cross_edges = []

    for u, v, data in G.edges(data=True):
        cid_u = node_community.get(u)
        cid_v = node_community.get(v)
        if cid_u is None or cid_v is None or cid_u == cid_v:
            continue

        score = 0
        reasons = []
        relations = data.get("relations", [])
        if isinstance(relations, list):
            if "same_collection" in relations:
                score += 2
                reasons.append("cross-collection connection")
            if "shared_tag" in relations:
                score += 1
                reasons.append("cross-community shared tag")

        deg_u, deg_v = G.degree(u), G.degree(v)
        if min(deg_u, deg_v) <= 2 and max(deg_u, deg_v) >= 5:
            score += 1
            peripheral = G.nodes[u].get("label", u) if deg_u <= 2 else G.nodes[v].get("label", v)
            hub = G.nodes[v].get("label", v) if deg_u <= 2 else G.nodes[u].get("label", u)
            reasons.append(f"peripheral '{peripheral}' connects hub '{hub}'")

        cross_edges.append({
            "_score": score,
            "source_label": G.nodes[u].get("label", u),
            "target_label": G.nodes[v].get("label", v),
            "relation": data.get("relation", ""),
            "shared_tags": data.get("shared_tags", []),
            "community_pair": tuple(sorted([cid_u, cid_v])),
            "why": "; ".join(reasons) if reasons else "cross-community edge",
        })

    cross_edges.sort(key=lambda x: x["_score"], reverse=True)
    seen_pairs: set[tuple] = set()
    deduped = []
    for edge in cross_edges:
        pair = edge["community_pair"]
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            deduped.append(edge)
        if len(deduped) >= top_n:
            break

    for edge in deduped:
        edge.pop("_score")

    return deduped


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_report(
    G: nx.Graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    cohesion: dict[int, float],
    collection_names: dict[str, str],
    validation: dict[str, Any],
    god_list: list[dict[str, Any]],
    surprise_list: list[dict[str, Any]],
    stats: dict[str, Any],
    output_dir: str,
) -> None:
    """Export graph results: JSON, report, and community summary."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- JSON export ---
    node_community = {n: cid for cid, nodes in communities.items() for n in nodes}
    graph_data = {
        "nodes": [
            {
                "id": key,
                "label": G.nodes[key].get("label", key),
                "item_type": G.nodes[key].get("item_type", ""),
                "tags_normalized": G.nodes[key].get("tags_normalized", []),
                "collections": G.nodes[key].get("collections", []),
                "community": node_community.get(key),
                "community_label": community_labels.get(node_community.get(key, -1), "Unknown"),
            }
            for key in G.nodes()
        ],
        "edges": [
            {
                "source": u,
                "target": v,
                "relation": data.get("relation", ""),
                "relations": data.get("relations", []),
                "shared_tags": data.get("shared_tags", []),
                "weight": data.get("weight", 1.0),
                "confidence": data.get("confidence", "EXTRACTED"),
            }
            for u, v, data in G.edges(data=True)
        ],
        "communities": {
            str(cid): {
                "label": community_labels.get(cid, f"Community {cid}"),
                "members": members,
                "cohesion": cohesion.get(cid, 0),
                "size": len(members),
            }
            for cid, members in communities.items()
        },
        "collection_names": collection_names,
        "stats": stats,
        "validation": validation,
    }
    with open(out / "graph.json", "w", encoding="utf-8") as f:
        json.dump(graph_data, f, indent=2, ensure_ascii=False)

    # --- Report ---
    lines = [
        "# Zotero Knowledge Graph Report",
        "",
        "## Graph Structure",
        f"- {stats['nodes']} items · {stats['edges']} connections · {len(communities)} communities",
        f"- Avg degree: {stats['avg_degree']} · Max degree: {stats['max_degree']}",
        f"- Isolated: {stats['isolated_nodes']} ({stats['isolated_pct']}%)",
        f"- Modularity: {validation['modularity']} · Avg cohesion: {validation['avg_cohesion']}",
        f"- Status: **{validation['status']}**",
    ]

    if validation.get("issues"):
        lines += ["", "## Issues"]
        for issue in validation["issues"]:
            lines.append(f"- {issue}")

    lines += ["", "## Communities"]
    for cid, members in communities.items():
        label = community_labels.get(cid, f"Community {cid}")
        coh = cohesion.get(cid, 0)
        # Show collection memberships for context
        coll_keys_in_community = set()
        for m in members[:10]:
            for ck in G.nodes[m].get("collections", []):
                coll_keys_in_community.add(ck)
        coll_names = [collection_names.get(ck, ck) for ck in coll_keys_in_community]
        lines.append(f"- **{label}** ({len(members)} items, cohesion: {coh}, collections: {coll_names})")

    lines += ["", "## God Nodes (most connected items)"]
    for i, node in enumerate(god_list[:10], 1):
        lines.append(f"{i}. `{node['label']}` — {node['edges']} connections")

    lines += ["", "## Surprising Connections (cross-community)"]
    if surprise_list:
        for s in surprise_list:
            lines.append(f"- `{s['source_label']}` ↔ `{s['target_label']}` — {s.get('why', 'cross-community')}")
    else:
        lines.append("- None detected")

    with open(out / "GRAPH_REPORT.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # --- Tag filter summary ---
    tag_report_lines = ["# Tag Filter Summary", ""]
    tag_counts = Counter()
    for key in G.nodes():
        for tag in G.nodes[key].get("tags_normalized", []):
            tag_counts[tag] += 1

    total = G.number_of_nodes()
    max_items = max(int(total * 0.30), 3)
    tag_report_lines.append(f"Total items: {total}, Max tag coverage: {max_items} ({0.30*100:.0f}%)")
    tag_report_lines.append("")
    tag_report_lines.append("## Valid tags")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        if count >= 2 and count <= max_items:
            tag_report_lines.append(f"- `{tag}`: {count} items")
    tag_report_lines.append("")
    tag_report_lines.append("## Filtered out (too broad)")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        if count > max_items:
            tag_report_lines.append(f"- `{tag}`: {count} items (> {max_items})")
    tag_report_lines.append("")
    tag_report_lines.append("## Filtered out (too narrow, only 1 item)")
    for tag, count in sorted(tag_counts.items(), key=lambda x: -x[1]):
        if count < 2:
            tag_report_lines.append(f"- `{tag}`: {count} item")

    with open(out / "TAG_FILTER.md", "w", encoding="utf-8") as f:
        f.write("\n".join(tag_report_lines))

    print(f"\nOutput written to {out}/")
    print(f"  - graph.json ({(out / 'graph.json').stat().st_size} bytes)")
    print(f"  - GRAPH_REPORT.md")
    print(f"  - TAG_FILTER.md")