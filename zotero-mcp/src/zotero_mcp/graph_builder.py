"""
Build a NetworkX graph from Zotero items using collection and tag relationships.

Two-layer edge construction:
  Layer 1 — same_collection (EXTRACTED, user-curated, strong signal)
  Layer 2 — shared_tag (EXTRACTED, LLM-generated tags, medium signal)

Anti-hairball controls:
  - Tag normalization (merge synonyms)
  - Granularity filtering (remove overly broad/narrow tags)
  - Degree cap (max edges per node)
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tag normalization
# ---------------------------------------------------------------------------

def normalize_tag(tag: str) -> str:
    """Normalize a tag for deduplication.

    - Lowercase
    - Strip leading/trailing whitespace and # prefix
    - Replace separators (/, -, _) with single hyphen
    - Collapse whitespace into single hyphen
    """
    t = tag.strip().lower()
    if t.startswith("#"):
        t = t[1:]
    # Normalize separators
    t = re.sub(r"[/\-_]+", "-", t)
    # Collapse whitespace
    t = re.sub(r"\s+", "-", t)
    return t.strip("-")


# ---------------------------------------------------------------------------
# Granularity filtering
# ---------------------------------------------------------------------------

class TagFilter:
    """Filter tags by granularity to prevent hairball graphs.

    Overly broad tags (covering > max_fraction of items) create dense connections.
    Overly narrow tags (covering only 1 item) produce no edges.
    """

    def __init__(
        self,
        max_fraction: float = 0.30,
        min_items: int = 2,
        max_items_override: int | None = None,
    ):
        self.max_fraction = max_fraction
        self.min_items = min_items
        self.max_items_override = max_items_override

    def filter_tags(
        self,
        tag_counts: Counter,
        total_items: int,
    ) -> set[str]:
        """Return the set of tags that pass granularity checks.

        Args:
            tag_counts: Counter mapping normalized tag -> number of items with that tag
            total_items: Total number of items in the graph

        Returns:
            Set of normalized tags that are neither too broad nor too narrow
        """
        max_items = (
            self.max_items_override
            if self.max_items_override is not None
            else int(total_items * self.max_fraction)
        )
        # Ensure at least min_items threshold for max
        max_items = max(max_items, self.min_items + 1)

        valid = set()
        for tag, count in tag_counts.items():
            if count < self.min_items:
                # Too narrow — only 1 item, produces no edges
                continue
            if count > max_items:
                # Too broad — would connect too many nodes
                logger.debug(f"Filtered overly broad tag: '{tag}' ({count}/{total_items} items)")
                continue
            valid.add(tag)
        return valid


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(
    items: list[dict[str, Any]],
    *,
    max_degree: int = 20,
    tag_filter: TagFilter | None = None,
    collection_weight: float = 2.0,
    tag_weight: float = 1.0,
) -> nx.Graph:
    """Build a NetworkX graph from Zotero items.

    Nodes are Zotero items (keyed by item_key).
    Edges are created from shared collections and shared tags.

    Args:
        items: List of item dicts, each containing:
            - "key": Zotero item key (used as node ID)
            - "title": Item title (used as node label)
            - "tags": List of tag dicts [{"tag": "..."}]
            - "collections": List of collection keys
            - "itemType": Item type string
        max_degree: Maximum number of edges per node. Higher-degree nodes
            keep only the highest-weight edges.
        tag_filter: TagFilter instance for granularity filtering.
            Default filters tags covering >30% or <2 items.
        collection_weight: Edge weight for same_collection edges.
        tag_weight: Edge weight for shared_tag edges.

    Returns:
        NetworkX Graph with item nodes and relationship edges.
    """
    if tag_filter is None:
        tag_filter = TagFilter()

    G = nx.Graph()

    # --- Phase 1: Add nodes ---
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
            source_file="",  # no file path for Zotero items
        )
        item_keys.add(key)

    if G.number_of_nodes() == 0:
        return G

    # --- Phase 2: same_collection edges ---
    # Group items by collection key
    collection_groups: dict[str, list[str]] = {}
    for key in item_keys:
        for coll_key in G.nodes[key].get("collections", []):
            collection_groups.setdefault(coll_key, []).append(key)

    for coll_key, members in collection_groups.items():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                u, v = members[i], members[j]
                if G.has_edge(u, v):
                    # Edge already exists (possibly from tag) — boost weight
                    G.edges[u, v]["weight"] += collection_weight
                    G.edges[u, v]["relations"].add("same_collection")
                else:
                    G.add_edge(
                        u, v,
                        weight=collection_weight,
                        relations={"same_collection"},
                        shared_tags=set(),
                        confidence="EXTRACTED",
                    )

    # --- Phase 3: shared_tag edges ---
    # Count tag frequencies for granularity filtering
    tag_counts = Counter()
    for key in item_keys:
        for tag in G.nodes[key].get("tags_normalized", []):
            tag_counts[tag] += 1

    valid_tags = tag_filter.filter_tags(tag_counts, len(item_keys))

    # Group items by valid normalized tag
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
                    # Edge already exists — add shared_tag relation, boost weight
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

    # --- Phase 4: Clean up edge attributes ---
    # Convert sets to sorted lists for JSON serialization
    for u, v, data in G.edges(data=True):
        data["relations"] = sorted(data.get("relations", set()))
        data["shared_tags"] = sorted(data.get("shared_tags", set()))
        # Primary relation label
        if "same_collection" in data["relations"]:
            data["relation"] = "same_collection"
        else:
            data["relation"] = "shared_tag"

    # --- Phase 5: Degree cap ---
    _apply_degree_cap(G, max_degree)

    logger.info(
        f"Built graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
        f"{len(valid_tags)} valid tags, {len(collection_groups)} collections"
    )
    return G


def _apply_degree_cap(G: nx.Graph, max_degree: int) -> None:
    """Trim edges from nodes that exceed max_degree.

    Keeps the highest-weight edges. If all edges have equal weight,
    keeps edges with the most shared_tags (more specific connections).
    """
    if max_degree <= 0:
        return

    over_degree = [
        (node, degree)
        for node, degree in G.degree()
        if degree > max_degree
    ]

    for node, _ in over_degree:
        # Sort edges by weight (descending), then by number of shared_tags (descending)
        edges = sorted(
            G.edges(node, data=True),
            key=lambda e: (
                -e[2].get("weight", 1.0),
                -len(e[2].get("shared_tags", [])),
            ),
        )
        # Remove lowest-weight edges beyond the cap
        for u, v, _ in edges[max_degree:]:
            G.remove_edge(u, v)


# ---------------------------------------------------------------------------
# Graph statistics (for validation)
# ---------------------------------------------------------------------------

def graph_stats(G: nx.Graph) -> dict[str, Any]:
    """Compute structural statistics for graph validation.

    Returns dict with metrics to evaluate whether the graph is
    structurally sound (not a hairball, not fragmented).
    """
    if G.number_of_nodes() == 0:
        return {
            "nodes": 0,
            "edges": 0,
            "avg_degree": 0,
            "max_degree": 0,
            "p90_degree": 0,
            "isolated_pct": 0,
            "status": "empty",
        }

    degrees = [d for _, d in G.degree()]
    isolated = [n for n in G.nodes() if G.degree(n) == 0]

    avg_deg = sum(degrees) / len(degrees) if degrees else 0
    sorted_deg = sorted(degrees)
    p90_idx = int(len(sorted_deg) * 0.9)
    p90_deg = sorted_deg[p90_idx] if sorted_deg else 0

    isolated_pct = len(isolated) / G.number_of_nodes() * 100

    # Determine status
    if avg_deg > 20:
        status = "too_dense"
    elif avg_deg < 2:
        status = "too_sparse"
    elif isolated_pct > 30:
        status = "too_fragmented"
    else:
        status = "healthy"

    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "avg_degree": round(avg_deg, 1),
        "max_degree": max(degrees) if degrees else 0,
        "p90_degree": p90_deg,
        "isolated_nodes": len(isolated),
        "isolated_pct": round(isolated_pct, 1),
        "status": status,
    }