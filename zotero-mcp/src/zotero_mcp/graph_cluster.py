"""
Community detection on NetworkX graphs for Zotero knowledge graphs.

Uses Leiden (graspologic) if available, falls back to Louvain (networkx).
Splits oversized communities. Returns cohesion scores and modularity.
"""
from __future__ import annotations

import contextlib
import inspect
import io
import logging
import sys

import networkx as nx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Partition algorithms
# ---------------------------------------------------------------------------

def _suppress_output():
    """Suppress stdout/stderr during library calls (graspologic emits ANSI)."""
    return contextlib.redirect_stdout(io.StringIO())


def _partition(G: nx.Graph) -> dict[str, int]:
    """Run community detection. Returns {node_id: community_id}.

    Tries Leiden (graspologic) first, falls back to Louvain (networkx).
    """
    try:
        from graspologic.partition import leiden
        old_stderr = sys.stderr
        try:
            sys.stderr = io.StringIO()
            with _suppress_output():
                result = leiden(G)
        finally:
            sys.stderr = old_stderr
        return result
    except ImportError:
        logger.info("graspologic not installed, falling back to Louvain")
    except Exception as e:
        logger.warning(f"Leiden failed ({e}), falling back to Louvain")

    # Fallback: networkx Louvain
    kwargs: dict = {"seed": 42, "threshold": 1e-4}
    if "max_level" in inspect.signature(nx.community.louvain_communities).parameters:
        kwargs["max_level"] = 10
    communities = nx.community.louvain_communities(G, **kwargs)
    return {node: cid for cid, nodes in enumerate(communities) for node in nodes}


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

_MAX_COMMUNITY_FRACTION = 0.25
_MIN_SPLIT_SIZE = 10


def cluster(G: nx.Graph) -> dict[int, list[str]]:
    """Run Leiden/Louvain community detection.

    Returns {community_id: [node_ids]} sorted by size descending.
    Oversized communities (>25% of nodes) are split with a second pass.
    """
    if G.number_of_nodes() == 0:
        return {}
    if G.is_directed():
        G = G.to_undirected()
    if G.number_of_edges() == 0:
        return {i: [n] for i, n in enumerate(sorted(G.nodes))}

    # Handle isolates separately (Louvain/Leiden drop them)
    isolates = [n for n in G.nodes() if G.degree(n) == 0]
    connected = G.subgraph([n for n in G.nodes() if G.degree(n) > 0])

    raw: dict[int, list[str]] = {}
    if connected.number_of_nodes() > 0:
        partition = _partition(connected)
        for node, cid in partition.items():
            raw.setdefault(cid, []).append(node)

    next_cid = max(raw.keys(), default=-1) + 1
    for node in isolates:
        raw[next_cid] = [node]
        next_cid += 1

    # Split oversized communities
    max_size = max(_MIN_SPLIT_SIZE, int(G.number_of_nodes() * _MAX_COMMUNITY_FRACTION))
    final_communities: list[list[str]] = []
    for nodes in raw.values():
        if len(nodes) > max_size:
            final_communities.extend(_split_community(G, nodes))
        else:
            final_communities.append(nodes)

    # Sort by size descending for deterministic ordering
    final_communities.sort(key=len, reverse=True)
    return {i: sorted(nodes) for i, nodes in enumerate(final_communities)}


def _split_community(G: nx.Graph, nodes: list[str]) -> list[list[str]]:
    """Split an oversized community with a second Leiden/Louvain pass."""
    subgraph = G.subgraph(nodes)
    if subgraph.number_of_edges() == 0:
        return [[n] for n in sorted(nodes)]
    try:
        sub_partition = _partition(subgraph)
        sub_communities: dict[int, list[str]] = {}
        for node, cid in sub_partition.items():
            sub_communities.setdefault(cid, []).append(node)
        if len(sub_communities) <= 1:
            return [sorted(nodes)]
        return [sorted(v) for v in sub_communities.values()]
    except Exception:
        return [sorted(nodes)]


# ---------------------------------------------------------------------------
# Cohesion & modularity
# ---------------------------------------------------------------------------

def cohesion_score(G: nx.Graph, community_nodes: list[str]) -> float:
    """Ratio of actual intra-community edges to maximum possible."""
    n = len(community_nodes)
    if n <= 1:
        return 1.0
    subgraph = G.subgraph(community_nodes)
    actual = subgraph.number_of_edges()
    possible = n * (n - 1) / 2
    return round(actual / possible, 2) if possible > 0 else 0.0


def score_all(G: nx.Graph, communities: dict[int, list[str]]) -> dict[int, float]:
    """Compute cohesion scores for all communities."""
    return {cid: cohesion_score(G, nodes) for cid, nodes in communities.items()}


def modularity(G: nx.Graph, communities: dict[int, list[str]]) -> float:
    """Compute graph modularity (how well the partition separates communities).

    > 0.3 → meaningful community structure
    < 0.1 → essentially no community structure
    """
    if G.number_of_edges() == 0 or len(communities) == 0:
        return 0.0
    partition = [set(nodes) for nodes in communities.values()]
    try:
        return round(nx.community.modularity(G, partition), 3)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Community labels
# ---------------------------------------------------------------------------

def label_communities(
    G: nx.Graph,
    communities: dict[int, list[str]],
) -> dict[int, str]:
    """Generate descriptive labels for each community.

    Label is derived from the most common tags shared by community members.
    Falls back to "Community {id}" if no tags are found.
    """
    labels: dict[int, str] = {}
    for cid, members in communities.items():
        # Collect all normalized tags from community members
        tag_counts: dict[str, int] = {}
        for node in members:
            if node not in G.nodes:
                continue
            for tag in G.nodes[node].get("tags_normalized", []):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

        if not tag_counts:
            labels[cid] = f"Community {cid}"
            continue

        # Pick the top 2 most common tags as the label
        top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:2]
        label_parts = [t for t, _ in top_tags]
        labels[cid] = " / ".join(label_parts)

    return labels


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_communities(
    G: nx.Graph,
    communities: dict[int, list[str]],
) -> dict[str, Any]:
    """Validate whether the community structure is effective.

    Checks:
    - Modularity (community separation quality)
    - Community size distribution (not too skewed)
    - Cohesion scores (communities are internally connected)
    - Isolated node ratio
    """
    from typing import Any

    mod = modularity(G, communities)
    cohesion = score_all(G, communities)

    sizes = [len(nodes) for nodes in communities.values()]
    total_nodes = G.number_of_nodes()
    max_community_pct = max(sizes) / total_nodes * 100 if total_nodes else 0
    isolated = len([n for n in G.nodes() if G.degree(n) == 0])
    isolated_pct = isolated / total_nodes * 100 if total_nodes else 0

    # Assessment
    issues = []
    if mod < 0.1:
        issues.append("Low modularity (< 0.1) — graph has no meaningful community structure")
    if max_community_pct > 80:
        issues.append(f"Giant community covers {max_community_pct:.0f}% of nodes — graph may be too dense")
    if isolated_pct > 30:
        issues.append(f"{isolated_pct:.0f}% isolated nodes — many items have no connections")
    avg_cohesion = sum(cohesion.values()) / len(cohesion) if cohesion else 0
    if avg_cohesion < 0.1:
        issues.append("Low average cohesion — communities are loosely connected internally")

    status = "healthy" if not issues else "needs_attention"

    return {
        "modularity": mod,
        "max_community_pct": round(max_community_pct, 1),
        "avg_cohesion": round(avg_cohesion, 2),
        "min_cohesion": min(cohesion.values()) if cohesion else 0,
        "isolated_pct": round(isolated_pct, 1),
        "community_count": len(communities),
        "community_sizes": sizes,
        "issues": issues,
        "status": status,
    }