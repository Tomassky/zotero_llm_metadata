"""Generate interactive HTML visualization from graph.json using Pyvis."""
from __future__ import annotations

import json
from pathlib import Path

from pyvis.network import Network


def visualize(graph_json_path: str, output_html: str = "graph/graph_vis.html") -> None:
    data = json.loads(Path(graph_json_path).read_text(encoding="utf-8"))

    node_community = {}
    community_labels = {}
    for cid_str, info in data["communities"].items():
        cid = int(cid_str)
        community_labels[cid] = info["label"]
        for member in info["members"]:
            node_community[member] = cid

    # Assign colors per community
    palette = [
        "#e6194b", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
        "#911eb4", "#42d4f4", "#f032e6", "#bfef45", "#fabed4",
        "#469990", "#dcbeff", "#9A6324", "#800000", "#aaffc3",
        "#808000", "#ffd8b1", "#000075", "#a9a9a9", "#e6beff",
    ]

    net = Network(
        height="900px", width="100%",
        bgcolor="#1a1a2e", font_color="white",
        directed=False, notebook=False,
    )
    net.force_atlas_2based(
        gravity=-50, central_gravity=0.01,
        spring_length=100, spring_strength=0.08,
        damping=0.4, overlap=0,
    )

    # Add nodes
    for node in data["nodes"]:
        nid = node["id"]
        cid = node.get("community")
        label = node.get("label", nid)
        # Truncate long labels for display
        short_label = label if len(label) <= 40 else label[:37] + "..."

        color = palette[cid % len(palette)] if cid is not None else "#666"

        title = (
            f"<b>{label}</b><br>"
            f"Type: {node.get('item_type', '')}<br>"
            f"Community: {community_labels.get(cid, 'Unknown')}<br>"
            f"Tags: {', '.join(node.get('tags_normalized', [])[:8])}<br>"
            f"Collections: {', '.join(node.get('collections', []))}"
        )

        net.add_node(
            nid,
            label=short_label,
            title=title,
            color=color,
            size=10,
        )

    # Add edges
    for edge in data["edges"]:
        rel = edge.get("relation", "")
        color = "#4363d8" if rel == "same_collection" else "#888"
        width = 2.0 if rel == "same_collection" else 0.5

        net.add_edge(
            edge["source"], edge["target"],
            color=color, width=width,
            title=f"{rel} | tags: {', '.join(edge.get('shared_tags', [])[:5])}",
        )

    net.show_buttons(filter_=["physics"])
    net.save_graph(output_html)
    print(f"Interactive graph saved to {output_html}")
    print("Open in browser to explore: drag nodes, hover for details, filter by physics.")


if __name__ == "__main__":
    visualize("graph/graph.json")