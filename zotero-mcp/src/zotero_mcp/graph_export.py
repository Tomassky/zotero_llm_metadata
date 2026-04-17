"""
Export Zotero knowledge graph to JSON and interactive HTML visualization.

Simplified from graphify's export.py, adapted for Zotero item context.
"""
from __future__ import annotations

import html as _html
import json
import logging
from pathlib import Path
from typing import Any

import networkx as nx
from networkx.readwrite import json_graph

from .graph_cluster import cohesion_score
from .graph_analyze import _node_community_map

logger = logging.getLogger(__name__)

COMMUNITY_COLORS = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
    "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC",
]


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------

def to_json(
    G: nx.Graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    cohesion_scores: dict[int, float],
    output_path: str,
) -> None:
    """Export graph as JSON with community metadata."""
    node_community = _node_community_map(communities)

    try:
        data = json_graph.node_link_data(G, edges="links")
    except TypeError:
        data = json_graph.node_link_data(G)

    for node in data["nodes"]:
        node["community"] = node_community.get(node["id"])
        node["community_label"] = community_labels.get(
            node["community"], f"Community {node['community']}"
        ) if node["community"] is not None else None

    for link in data["links"]:
        link["confidence"] = link.get("confidence", "EXTRACTED")

    data["communities"] = {
        str(cid): {
            "label": community_labels.get(cid, f"Community {cid}"),
            "members": nodes,
            "cohesion": cohesion_scores.get(cid, 0),
            "size": len(nodes),
        }
        for cid, nodes in communities.items()
    }

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# HTML export (vis.js interactive visualization)
# ---------------------------------------------------------------------------

_MAX_NODES_FOR_VIZ = 5_000


def _html_styles() -> str:
    return """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; height: 100vh; overflow: hidden; }
  #graph { flex: 1; }
  #sidebar { width: 280px; background: #1a1a2e; border-left: 1px solid #2a2a4e; display: flex; flex-direction: column; overflow: hidden; }
  #search-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
  #search { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 7px 10px; border-radius: 6px; font-size: 13px; outline: none; }
  #search:focus { border-color: #4E79A7; }
  #search-results { max-height: 140px; overflow-y: auto; padding: 4px 12px; border-bottom: 1px solid #2a2a4e; display: none; }
  .search-item { padding: 4px 6px; cursor: pointer; border-radius: 4px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .search-item:hover { background: #2a2a4e; }
  #info-panel { padding: 14px; border-bottom: 1px solid #2a2a4e; min-height: 140px; }
  #info-panel h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #info-content { font-size: 13px; color: #ccc; line-height: 1.6; }
  #info-content .field { margin-bottom: 5px; }
  #info-content .field b { color: #e0e0e0; }
  #info-content .empty { color: #555; font-style: italic; }
  .neighbor-link { display: block; padding: 2px 6px; margin: 2px 0; border-radius: 3px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-left: 3px solid #333; }
  .neighbor-link:hover { background: #2a2a4e; }
  #neighbors-list { max-height: 160px; overflow-y: auto; margin-top: 4px; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 12px; }
  #legend-wrap h3 { font-size: 13px; color: #aaa; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.05em; }
  .legend-item { display: flex; align-items: center; gap: 8px; padding: 4px 0; cursor: pointer; border-radius: 4px; font-size: 12px; }
  .legend-item:hover { background: #2a2a4e; padding-left: 4px; }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend-count { color: #666; font-size: 11px; }
  #stats { padding: 10px 14px; border-top: 1px solid #2a2a4e; font-size: 11px; color: #555; }
</style>"""


def _html_script(nodes_json: str, edges_json: str, legend_json: str) -> str:
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};

function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  _community: n.community, _community_name: n.community_name,
  _tags: n.tags, _degree: n.degree,
}})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({{
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
}})));

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes: nodesDS, edges: edgesDS }}, {{
  physics: {{
    enabled: true,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{
      gravitationalConstant: -60,
      centralGravity: 0.005,
      springLength: 120,
      springConstant: 0.08,
      damping: 0.4,
      avoidOverlap: 0.8,
    }},
    stabilization: {{ iterations: 200, fit: true }},
  }},
  interaction: {{ hover: true, tooltipDelay: 100, hideEdgesOnDrag: true }},
  nodes: {{ shape: 'dot', borderWidth: 1.5 }},
  edges: {{ smooth: {{ type: 'continuous', roundness: 0.2 }}, selectionWidth: 3 }},
}});

network.once('stabilizationIterationsDone', () => {{
  network.setOptions({{ physics: {{ enabled: false }} }});
}});

function showInfo(nodeId) {{
  const n = nodesDS.get(nodeId);
  if (!n) return;
  const neighborIds = network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.map(nid => {{
    const nb = nodesDS.get(nid);
    const color = nb ? nb.color.background : '#555';
    return `<span class="neighbor-link" style="border-left-color:${{esc(color)}}" onclick="focusNode(${{JSON.stringify(nid)}})">${{esc(nb ? nb.label : nid)}}</span>`;
  }}).join('');
  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${{esc(n.label)}}</b></div>
    <div class="field">Community: ${{esc(n._community_name)}}</div>
    <div class="field">Tags: ${{esc((n._tags || []).join(', '))}}</div>
    <div class="field">Degree: ${{n._degree}}</div>
    ${{neighborIds.length ? `<div class="field" style="margin-top:8px;color:#aaa;font-size:11px">Neighbors (${{neighborIds.length}})</div><div id="neighbors-list">${{neighborItems}}</div>` : ''}}
  `;
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.4, animation: true }});
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}}

let hoveredNodeId = null;
network.on('hoverNode', params => {{ hoveredNodeId = params.node; container.style.cursor = 'pointer'; }});
network.on('blurNode', () => {{ hoveredNodeId = null; container.style.cursor = 'default'; }});
container.addEventListener('click', () => {{
  if (hoveredNodeId !== null) {{ showInfo(hoveredNodeId); network.selectNodes([hoveredNodeId]); }}
}});
network.on('click', params => {{
  if (params.nodes.length > 0) {{ showInfo(params.nodes[0]); }}
  else if (hoveredNodeId === null) {{ document.getElementById('info-content').innerHTML = '<span class="empty">Click a node to inspect it</span>'; }}
}});

const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.toLowerCase().trim();
  searchResults.innerHTML = '';
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const matches = RAW_NODES.filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
  if (!matches.length) {{ searchResults.style.display = 'none'; return; }}
  searchResults.style.display = 'block';
  matches.forEach(n => {{
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label;
    el.style.borderLeft = `3px solid ${{n.color.background}}`;
    el.style.paddingLeft = '8px';
    el.onclick = () => {{
      network.focus(n.id, {{ scale: 1.5, animation: true }});
      network.selectNodes([n.id]);
      showInfo(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    }};
    searchResults.appendChild(el);
  }});
}});

const hiddenCommunities = new Set();
const legendEl = document.getElementById('legend');
LEGEND.forEach(c => {{
  const item = document.createElement('div');
  item.className = 'legend-item';
  item.innerHTML = `<div class="legend-dot" style="background:${{c.color}}"></div>
    <span class="legend-label">${{c.label}}</span>
    <span class="legend-count">${{c.count}}</span>`;
  item.onclick = () => {{
    if (hiddenCommunities.has(c.cid)) {{
      hiddenCommunities.delete(c.cid);
      item.classList.remove('dimmed');
    }} else {{
      hiddenCommunities.add(c.cid);
      item.classList.add('dimmed');
    }}
    const updates = RAW_NODES
      .filter(n => n.community === c.cid)
      .map(n => ({{ id: n.id, hidden: hiddenCommunities.has(c.cid) }}));
    nodesDS.update(updates);
  }};
  legendEl.appendChild(item);
}});
</script>"""


def to_html(
    G: nx.Graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    output_path: str,
    cohesion_scores: dict[int, float] | None = None,
) -> None:
    """Generate an interactive vis.js HTML visualization of the Zotero graph.

    Features: node size by degree, click-to-inspect, search box,
    community filter, physics clustering.
    """
    if G.number_of_nodes() > _MAX_NODES_FOR_VIZ:
        raise ValueError(
            f"Graph has {G.number_of_nodes()} nodes — too large for HTML viz. "
            "Use JSON export instead."
        )

    node_community = _node_community_map(communities)
    degree = dict(G.degree())
    max_deg = max(degree.values(), default=1) or 1

    # Build vis nodes
    vis_nodes = []
    for node_id, data in G.nodes(data=True):
        cid = node_community.get(node_id, 0)
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        label = data.get("label", node_id)
        deg = degree.get(node_id, 0)
        size = 10 + 30 * (deg / max_deg)
        font_size = 12 if deg >= max_deg * 0.15 else 0

        vis_nodes.append({
            "id": node_id,
            "label": label,
            "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#ffffff"},
            "title": _html.escape(label),
            "community": cid,
            "community_name": community_labels.get(cid, f"Community {cid}"),
            "tags": data.get("tags_normalized", []),
            "degree": deg,
        })

    # Build vis edges
    vis_edges = []
    for u, v, data in G.edges(data=True):
        relations = data.get("relations", [])
        relation_str = ", ".join(relations) if isinstance(relations, list) else str(relations)
        shared_tags = data.get("shared_tags", [])
        tags_str = ", ".join(shared_tags) if isinstance(shared_tags, list) else ""

        title_parts = [f"{relation_str} [EXTRACTED]"]
        if tags_str:
            title_parts.append(f"tags: {tags_str}")

        vis_edges.append({
            "from": u,
            "to": v,
            "title": _html.escape(" | ".join(title_parts)),
            "dashes": False,  # All EXTRACTED edges — solid
            "width": 2 if "same_collection" in relations else 1,
            "color": {"opacity": 0.7 if "same_collection" in relations else 0.4},
        })

    # Build legend
    legend_data = []
    for cid in sorted(communities.keys()):
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        lbl = community_labels.get(cid, f"Community {cid}")
        n = len(communities.get(cid, []))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": n})

    # JSON-serialize with </script> protection
    def _js_safe(obj) -> str:
        return json.dumps(obj).replace("</", "<\\/")

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)
    stats = f"{G.number_of_nodes()} nodes &middot; {G.number_of_edges()} edges &middot; {len(communities)} communities"

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Zotero Knowledge Graph</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
{_html_styles()}
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search items..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Item Info</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="legend-wrap">
    <h3>Communities</h3>
    <div id="legend"></div>
  </div>
  <div id="stats">{stats}</div>
</div>
{_html_script(nodes_json, edges_json, legend_json)}
</body>
</html>"""

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(
    G: nx.Graph,
    communities: dict[int, list[str]],
    community_labels: dict[int, str],
    cohesion_scores: dict[int, float],
    validation: dict[str, Any],
    god_node_list: list[dict[str, Any]],
    surprise_list: list[dict[str, Any]],
    question_list: list[dict[str, Any]],
) -> str:
    """Generate a plain-text report summarizing the graph analysis."""

    lines = [
        "# Zotero Knowledge Graph Report",
        "",
        "## Graph Structure",
        f"- {G.number_of_nodes()} items · {G.number_of_edges()} connections · {len(communities)} communities detected",
        f"- Modularity: {validation.get('modularity', 0)}",
        f"- Average cohesion: {validation.get('avg_cohesion', 0)}",
        f"- Isolated items: {validation.get('isolated_pct', 0)}%",
        f"- Status: {validation.get('status', 'unknown')}",
    ]

    if validation.get("issues"):
        lines.append("")
        lines.append("## Issues")
        for issue in validation["issues"]:
            lines.append(f"- {issue}")

    lines += [
        "",
        "## Communities",
    ]
    for cid, members in communities.items():
        label = community_labels.get(cid, f"Community {cid}")
        cohesion = cohesion_scores.get(cid, 0)
        lines.append(f"- **{label}** ({len(members)} items, cohesion: {cohesion})")

    lines += [
        "",
        "## God Nodes (most connected — your core items)",
    ]
    for i, node in enumerate(god_node_list, 1):
        lines.append(f"{i}. `{node['label']}` — {node['edges']} connections")

    lines += [
        "",
        "## Surprising Connections (cross-community)",
    ]
    if surprise_list:
        for s in surprise_list:
            lines.append(f"- `{s['source_label']}` ↔ `{s['target_label']}` — {s.get('why', 'cross-community')}")
    else:
        lines.append("- No cross-community connections detected")

    lines += [
        "",
        "## Suggested Questions",
    ]
    for q in question_list:
        if q.get("question"):
            lines.append(f"- {q['question']} ({q.get('why', '')})")

    return "\n".join(lines)