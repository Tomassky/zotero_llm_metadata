"""
Graph analysis for Zotero knowledge graphs.

Provides:
  - God nodes (most connected items — core literature)
  - Surprising connections (cross-community edges)
  - Suggested questions the graph can answer
"""
from __future__ import annotations

import logging
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# God nodes
# ---------------------------------------------------------------------------

def god_nodes(G: nx.Graph, top_n: int = 10) -> list[dict[str, Any]]:
    """Return the top_n most-connected items — the core literature.

    These are items that bridge multiple topics/tags/collections.
    """
    if G.number_of_nodes() == 0:
        return []

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
            "collections": G.nodes[node_id].get("collections", []),
        })
        if len(result) >= top_n:
            break
    return result


# ---------------------------------------------------------------------------
# Surprising connections
# ---------------------------------------------------------------------------

def _node_community_map(communities: dict[int, list[str]]) -> dict[str, int]:
    """Invert communities dict: node_id -> community_id."""
    return {n: cid for cid, nodes in communities.items() for n in nodes}


def surprising_connections(
    G: nx.Graph,
    communities: dict[int, list[str]],
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Find connections that bridge different communities.

    These are surprising because Leiden grouped everything else tightly —
    these edges cut across the natural structure, revealing cross-topic links.
    """
    if not communities or G.number_of_edges() == 0:
        return []

    node_community = _node_community_map(communities)

    # Find cross-community edges
    cross_edges = []
    for u, v, data in G.edges(data=True):
        cid_u = node_community.get(u)
        cid_v = node_community.get(v)
        if cid_u is None or cid_v is None or cid_u == cid_v:
            continue

        # Score based on edge properties
        score = 0
        reasons: list[str] = []

        # Weight by relation type — same_collection is stronger signal
        relations = data.get("relations", [])
        if isinstance(relations, list):
            if "same_collection" in relations:
                score += 2
                reasons.append("cross-collection connection")
            if "shared_tag" in relations:
                score += 1
                reasons.append("cross-community shared tag")

        # Peripheral-to-hub bonus
        deg_u = G.degree(u)
        deg_v = G.degree(v)
        if min(deg_u, deg_v) <= 2 and max(deg_u, deg_v) >= 5:
            score += 1
            peripheral = G.nodes[u].get("label", u) if deg_u <= 2 else G.nodes[v].get("label", v)
            hub = G.nodes[v].get("label", v) if deg_u <= 2 else G.nodes[u].get("label", u)
            reasons.append(f"peripheral item '{peripheral}' unexpectedly connects to hub '{hub}'")

        # Shared tag specificity — fewer items sharing the tag = more surprising
        shared_tags = data.get("shared_tags", [])
        if isinstance(shared_tags, list) and len(shared_tags) > 0:
            # Count how many nodes share each of these tags
            min_tag_coverage = min(
                sum(1 for n in G.nodes() if tag in G.nodes[n].get("tags_normalized", []))
                for tag in shared_tags
                if any(tag in G.nodes[n].get("tags_normalized", []) for n in G.nodes())
            )
            if min_tag_coverage <= 5:
                score += 2
                reasons.append("rare shared tag (< 5 items)")
            elif min_tag_coverage <= 10:
                score += 1
                reasons.append("uncommon shared tag (< 10 items)")

        cross_edges.append({
            "_score": score,
            "source_key": u,
            "source_label": G.nodes[u].get("label", u),
            "target_key": v,
            "target_label": G.nodes[v].get("label", v),
            "relation": data.get("relation", ""),
            "shared_tags": data.get("shared_tags", []),
            "community_pair": (cid_u, cid_v),
            "why": "; ".join(reasons) if reasons else "cross-community edge",
        })

    # Sort by surprise score, deduplicate by community pair
    cross_edges.sort(key=lambda x: x["_score"], reverse=True)
    seen_pairs: set[tuple] = set()
    deduped = []
    for edge in cross_edges:
        pair = tuple(sorted(edge["community_pair"]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            deduped.append(edge)
        if len(deduped) >= top_n:
            break

    # Remove internal scoring field
    for edge in deduped:
        edge.pop("_score")

    return deduped


# ---------------------------------------------------------------------------
# Bridge nodes
# ---------------------------------------------------------------------------

def bridge_nodes(
    G: nx.Graph,
    communities: dict[int, list[str]],
    top_n: int = 5,
) -> list[dict[str, Any]]:
    """Find nodes with high betweenness centrality that bridge communities.

    These are items that connect different research topics.
    """
    if G.number_of_edges() == 0:
        return []

    node_community = _node_community_map(communities)

    k = min(100, G.number_of_nodes()) if G.number_of_nodes() > 1000 else None
    betweenness = nx.betweenness_centrality(G, k=k)

    bridges = sorted(
        [(n, s) for n, s in betweenness.items() if s > 0],
        key=lambda x: x[1],
        reverse=True,
    )[:top_n]

    result = []
    for node_id, score in bridges:
        cid = node_community.get(node_id)
        # Count distinct communities this node connects to
        neighbor_comms = {
            node_community.get(nb)
            for nb in G.neighbors(node_id)
            if node_community.get(nb) != cid
        }
        result.append({
            "key": node_id,
            "label": G.nodes[node_id].get("label", node_id),
            "betweenness": round(score, 4),
            "connects_to_communities": len(neighbor_comms),
        })
    return result


# ---------------------------------------------------------------------------
# Suggested questions
# ---------------------------------------------------------------------------

def suggest_questions(
    G: nx.Graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    top_n: int = 7,
) -> list[dict[str, Any]]:
    """Generate questions the graph is uniquely positioned to answer."""
    questions = []
    node_community = _node_community_map(communities)

    # 1. Bridge node questions
    bridges = bridge_nodes(G, communities, top_n=3)
    for b in bridges:
        label = b["label"]
        cid = node_community.get(b["key"])
        comm_label = community_labels.get(cid, f"Community {cid}") if cid is not None else "unknown"
        questions.append({
            "type": "bridge_node",
            "question": f"Why does '{label}' connect {comm_label} to {b['connects_to_communities']} other communities?",
            "why": f"High betweenness ({b['betweenness']}) — this item bridges research topics.",
        })

    # 2. Surprising connections questions
    surprises = surprising_connections(G, communities, top_n=3)
    for s in surprises:
        questions.append({
            "type": "cross_community",
            "question": f"What connects '{s['source_label']}' and '{s['target_label']}' across different topics?",
            "why": s.get("why", "Cross-community connection"),
        })

    # 3. Isolated node exploration
    isolated = [n for n in G.nodes() if G.degree(n) == 0]
    if isolated:
        labels = [G.nodes[n].get("label", n) for n in isolated[:3]]
        quoted_labels = ['"' + l + '"' for l in labels]
        questions.append({
            "type": "isolated_nodes",
            "question": f"What topics connect {', '.join(quoted_labels)} to the rest of the library?",
            "why": f"{len(isolated)} items have no connections — possible gaps in tagging.",
        })

    # 4. Low-cohesion communities
    from .graph_cluster import cohesion_score
    for cid, nodes in communities.items():
        if len(nodes) >= 5:
            score = cohesion_score(G, nodes)
            if score < 0.15:
                label = community_labels.get(cid, f"Community {cid}")
                questions.append({
                    "type": "low_cohesion",
                    "question": f"Should '{label}' be split into smaller, more focused topics?",
                    "why": f"Cohesion {score} — items in this topic are weakly interconnected.",
                })

    # 5. God node verification
    top_gods = god_nodes(G, top_n=3)
    for g in top_gods:
        if g["edges"] >= 5:
            questions.append({
                "type": "core_item",
                "question": f"Is '{g['label']}' truly a core item that connects multiple topics?",
                "why": f"Degree {g['edges']} — highly connected, may be a hub or just broadly tagged.",
            })

    if not questions:
        questions.append({
            "type": "no_signal",
            "question": None,
            "why": "Not enough signal. Try relaxing tag filters or adding more items.",
        })

    return questions[:top_n]