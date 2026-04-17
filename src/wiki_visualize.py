#!/usr/bin/env python3
"""
wiki_visualize.py -- Interactive knowledge graph visualization using plotly.

The full graph (2,167 nodes, 593K edges) is too large to render at once.
Users MUST specify boundaries to get a feasible visualization.

Usage:
    # Show neighborhood around specific skills (most common)
    python wiki_visualize.py --seed fastapi-pro,docker-expert --hops 1

    # Show a single community
    python wiki_visualize.py --community 0

    # Show top-N most connected nodes
    python wiki_visualize.py --top 50

    # Filter by tag
    python wiki_visualize.py --tag security --top 30

    # Filter by minimum edge weight
    python wiki_visualize.py --seed python-pro --hops 2 --min-weight 3

    # Save to HTML file instead of opening browser
    python wiki_visualize.py --seed fastapi-pro --hops 1 --output graph.html
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

try:
    import networkx as nx
    from networkx.readwrite import node_link_graph
    import plotly.graph_objects as go
except ImportError:
    print("Required: pip install networkx plotly", file=sys.stderr)
    sys.exit(1)

WIKI_DIR = Path(os.path.expanduser("~/.claude/skill-wiki"))
GRAPH_PATH = WIKI_DIR / "graphify-out" / "graph.json"
COMMUNITIES_PATH = WIKI_DIR / "graphify-out" / "communities.json"

# Node colors by type
TYPE_COLORS = {
    "skill": "#6366f1",   # indigo
    "agent": "#f59e0b",   # amber
}

# Community color palette
COMMUNITY_COLORS = [
    "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4",
    "#3b82f6", "#8b5cf6", "#ec4899", "#14b8a6", "#f43f5e",
    "#84cc16", "#0ea5e9", "#a855f7", "#e11d48", "#10b981",
]


def load_graph() -> nx.Graph:
    """Load the knowledge graph."""
    if not GRAPH_PATH.exists():
        print(f"Error: {GRAPH_PATH} not found. Run wiki_graphify.py first.", file=sys.stderr)
        sys.exit(1)
    with open(GRAPH_PATH, encoding="utf-8") as f:
        return node_link_graph(json.load(f))


def load_communities() -> dict:
    """Load community assignments."""
    if not COMMUNITIES_PATH.exists():
        return {}
    with open(COMMUNITIES_PATH, encoding="utf-8") as f:
        return json.load(f)


def extract_subgraph(
    G: nx.Graph,
    *,
    seeds: list[str] | None = None,
    hops: int = 1,
    min_weight: int = 1,
    community_id: int | None = None,
    tag_filter: str | None = None,
    top_n: int | None = None,
) -> nx.Graph:
    """Extract a viewable subgraph based on user boundaries."""
    # Start with candidate nodes
    if seeds:
        # Find seed nodes by label match
        seed_ids: set[str] = set()
        for seed in seeds:
            for nid, data in G.nodes(data=True):
                if data.get("label", "") == seed or nid.endswith(f":{seed}"):
                    seed_ids.add(nid)
        if not seed_ids:
            print(f"Warning: no nodes found matching seeds: {seeds}", file=sys.stderr)
            return nx.Graph()

        # BFS to collect neighborhood
        nodes: set[str] = set(seed_ids)
        frontier = list(seed_ids)
        for _ in range(hops):
            next_frontier: list[str] = []
            for nid in frontier:
                for neighbor in G.neighbors(nid):
                    edge_data = G[nid][neighbor]
                    if edge_data.get("weight", 1) >= min_weight and neighbor not in nodes:
                        nodes.add(neighbor)
                        next_frontier.append(neighbor)
            frontier = next_frontier

    elif community_id is not None:
        communities = load_communities()
        comm_data = communities.get("communities", {}).get(str(community_id))
        if not comm_data:
            print(f"Error: community {community_id} not found", file=sys.stderr)
            return nx.Graph()
        nodes = set(comm_data.get("members", []))

    elif tag_filter:
        nodes = {
            nid for nid, data in G.nodes(data=True)
            if tag_filter in data.get("tags", [])
        }

    else:
        nodes = set(G.nodes())

    # Apply top-N filter by degree within the candidate set
    if top_n and len(nodes) > top_n:
        ranked = sorted(nodes, key=lambda n: G.degree(n), reverse=True)
        nodes = set(ranked[:top_n])

    # Build subgraph
    sub = G.subgraph(nodes).copy()

    # Filter edges by min_weight
    if min_weight > 1:
        weak_edges = [
            (u, v) for u, v, d in sub.edges(data=True)
            if d.get("weight", 1) < min_weight
        ]
        sub.remove_edges_from(weak_edges)

    # Remove isolated nodes after edge filtering
    isolated = [n for n in sub.nodes() if sub.degree(n) == 0]
    sub.remove_nodes_from(isolated)

    return sub


def compute_layout(G: nx.Graph) -> dict[str, tuple[float, float]]:
    """Compute node positions using spring layout."""
    if G.number_of_nodes() == 0:
        return {}
    return nx.spring_layout(G, k=2.0 / math.sqrt(max(G.number_of_nodes(), 1)), iterations=50, seed=42)


def build_html_with_filters(G: nx.Graph, pos: dict, title: str = "Knowledge Graph") -> str:
    """Build a self-contained HTML page with sidebar filter controls."""
    # Prepare node data as JSON for the JS frontend
    nodes_data = []
    communities = load_communities()
    node_community: dict[str, int] = {}
    for cid, comm_data in communities.get("communities", {}).items():
        for member in comm_data.get("members", []):
            node_community[member] = int(cid)

    tag_counts: dict[str, int] = defaultdict(int)
    for nid in G.nodes():
        if nid not in pos:
            continue
        data = G.nodes[nid]
        label = data.get("label", nid.split(":", 1)[-1])
        node_type = data.get("type", "skill")
        tags = data.get("tags", [])
        degree = G.degree(nid)
        cid = node_community.get(nid, 0)
        for t in tags:
            if t != "uncategorized":
                tag_counts[t] += 1
        nodes_data.append({
            "id": nid, "label": label, "type": node_type,
            "x": round(pos[nid][0], 4), "y": round(pos[nid][1], 4),
            "degree": degree, "tags": tags, "community": cid,
            "size": max(6, min(35, degree * 0.4 + 4)),
        })

    edges_data = []
    for u, v, d in G.edges(data=True):
        if u in pos and v in pos:
            edges_data.append({
                "source": u, "target": v,
                "weight": d.get("weight", 1),
            })

    top_tags = sorted(tag_counts.items(), key=lambda x: -x[1])[:25]

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ display:flex; height:100vh; background:#0f172a; color:#e2e8f0; font-family:system-ui; }}
  #sidebar {{ width:280px; padding:16px; overflow-y:auto; background:#1e293b; border-right:1px solid #334155; }}
  #graph {{ flex:1; }}
  h2 {{ font-size:14px; margin:12px 0 6px; color:#94a3b8; text-transform:uppercase; letter-spacing:1px; }}
  .filter-group {{ margin-bottom:12px; }}
  input[type=text] {{ width:100%; padding:6px 8px; background:#0f172a; border:1px solid #475569; color:#e2e8f0; border-radius:4px; font-size:13px; position:relative; }}
  #autocomplete {{ position:absolute; left:16px; right:16px; background:#1e293b; border:1px solid #475569; border-top:none; border-radius:0 0 4px 4px; max-height:200px; overflow-y:auto; z-index:10; display:none; }}
  .ac-item {{ padding:4px 8px; font-size:12px; cursor:pointer; color:#94a3b8; }}
  .ac-item:hover {{ background:#334155; color:#e2e8f0; }}
  .ac-item .ac-type {{ font-size:10px; color:#64748b; float:right; }}
  input[type=range] {{ width:100%; accent-color:#6366f1; }}
  label {{ display:block; font-size:12px; margin:2px 0; cursor:pointer; }}
  label:hover {{ color:#818cf8; }}
  .tag-btn {{ display:inline-block; padding:2px 8px; margin:2px; font-size:11px; border:1px solid #475569; border-radius:12px; cursor:pointer; background:transparent; color:#94a3b8; }}
  .tag-btn.active {{ background:#6366f1; color:white; border-color:#6366f1; }}
  .stat {{ font-size:12px; color:#64748b; margin:4px 0; }}
  #title {{ font-size:15px; font-weight:bold; color:#e2e8f0; margin-bottom:12px; }}
  .legend {{ display:flex; gap:12px; margin:8px 0; }}
  .legend-item {{ display:flex; align-items:center; gap:4px; font-size:11px; }}
  .legend-dot {{ width:10px; height:10px; border-radius:50%; }}
</style>
</head><body>
<div id="sidebar">
  <div id="title">{title}</div>
  <div class="stat" id="stat-line">Loading...</div>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#6366f1"></div>Skills</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div>Agents</div>
  </div>

  <h2>Search</h2>
  <div class="filter-group" style="position:relative">
    <input type="text" id="search" placeholder="Search by name..." oninput="onSearch()" autocomplete="off">
    <div id="autocomplete"></div>
  </div>

  <h2>Node Type</h2>
  <div class="filter-group">
    <label><input type="checkbox" id="show-skills" checked onchange="applyFilters()"> Skills</label>
    <label><input type="checkbox" id="show-agents" checked onchange="applyFilters()"> Agents</label>
  </div>

  <h2>Min Connections: <span id="deg-val">1</span></h2>
  <div class="filter-group">
    <input type="range" id="min-degree" min="1" max="50" value="1" oninput="document.getElementById('deg-val').textContent=this.value; applyFilters()">
  </div>

  <h2>Tags</h2>
  <div class="filter-group" id="tag-filters">
    {''.join(f'<span class="tag-btn" onclick="toggleTag(this)" data-tag="{t}">{t} ({c})</span>' for t, c in top_tags)}
  </div>

  <h2>Labels</h2>
  <div class="filter-group">
    <label><input type="checkbox" id="show-labels" checked onchange="applyFilters()"> Show labels</label>
  </div>
</div>
<div id="graph"></div>

<script>
const NODES = {json.dumps(nodes_data)};
const EDGES = {json.dumps(edges_data)};
const TYPE_COLORS = {{"skill": "#6366f1", "agent": "#f59e0b"}};
const COMM_COLORS = ["#ef4444","#f97316","#eab308","#22c55e","#06b6d4","#3b82f6","#8b5cf6","#ec4899","#14b8a6","#f43f5e","#84cc16","#0ea5e9","#a855f7","#e11d48","#10b981"];

let activeTags = new Set();
const allLabels = NODES.map(n => ({{label:n.label, type:n.type}})).sort((a,b) => a.label.localeCompare(b.label));

function onSearch() {{
  const q = document.getElementById('search').value.toLowerCase();
  const ac = document.getElementById('autocomplete');
  if (q.length < 2) {{ ac.style.display='none'; applyFilters(); return; }}
  const matches = allLabels.filter(n => n.label.toLowerCase().includes(q)).slice(0, 12);
  if (matches.length === 0) {{ ac.style.display='none'; applyFilters(); return; }}
  // Build DOM nodes with textContent to prevent XSS (labels may contain <,>,&,",').
  while (ac.firstChild) ac.removeChild(ac.firstChild);
  for (const m of matches) {{
    const item = document.createElement('div');
    item.className = 'ac-item';
    item.appendChild(document.createTextNode(m.label));
    const typeSpan = document.createElement('span');
    typeSpan.className = 'ac-type';
    typeSpan.textContent = m.type;
    item.appendChild(typeSpan);
    item.addEventListener('click', () => selectAc(m.label));
    ac.appendChild(item);
  }}
  ac.style.display='block';
  applyFilters();
}}

function selectAc(label) {{
  document.getElementById('search').value = label;
  document.getElementById('autocomplete').style.display='none';
  applyFilters();
}}

document.addEventListener('click', function(e) {{
  if (!e.target.closest('#search') && !e.target.closest('#autocomplete'))
    document.getElementById('autocomplete').style.display='none';
}});

function toggleTag(el) {{
  const tag = el.dataset.tag;
  if (activeTags.has(tag)) {{ activeTags.delete(tag); el.classList.remove('active'); }}
  else {{ activeTags.add(tag); el.classList.add('active'); }}
  applyFilters();
}}

function applyFilters() {{
  const search = document.getElementById('search').value.toLowerCase();
  const showSkills = document.getElementById('show-skills').checked;
  const showAgents = document.getElementById('show-agents').checked;
  const minDeg = parseInt(document.getElementById('min-degree').value);
  const showLabels = document.getElementById('show-labels').checked;

  const visible = new Set();
  const filtered = NODES.filter(n => {{
    if (!showSkills && n.type === 'skill') return false;
    if (!showAgents && n.type === 'agent') return false;
    if (n.degree < minDeg) return false;
    if (search && !n.label.toLowerCase().includes(search)) return false;
    if (activeTags.size > 0 && !n.tags.some(t => activeTags.has(t))) return false;
    visible.add(n.id);
    return true;
  }});

  const edgeX = [], edgeY = [];
  let edgeCount = 0;
  EDGES.forEach(e => {{
    if (visible.has(e.source) && visible.has(e.target)) {{
      const s = NODES.find(n => n.id === e.source);
      const t = NODES.find(n => n.id === e.target);
      if (s && t) {{ edgeX.push(s.x, t.x, null); edgeY.push(s.y, t.y, null); edgeCount++; }}
    }}
  }});

  const traces = [{{
    x: edgeX, y: edgeY, mode: 'lines',
    line: {{ width: 0.3, color: '#334155' }},
    hoverinfo: 'none', showlegend: false
  }}];

  ['skill','agent'].forEach(type => {{
    const tn = filtered.filter(n => n.type === type);
    if (!tn.length) return;
    traces.push({{
      x: tn.map(n=>n.x), y: tn.map(n=>n.y),
      mode: showLabels ? 'markers+text' : 'markers',
      name: type + 's (' + tn.length + ')',
      marker: {{ size: tn.map(n=>n.size), color: tn.map(n=>COMM_COLORS[n.community%15]),
                 line: {{ width: 1, color: '#1e293b' }}, opacity: 0.85 }},
      text: tn.map(n=>n.label),
      textposition: 'top center',
      textfont: {{ size: 7, color: '#94a3b8' }},
      hovertext: tn.map(n => '<b>'+n.label+'</b><br>Type: '+n.type+'<br>Connections: '+n.degree+'<br>Tags: '+n.tags.slice(0,5).join(', ')+'<br>Community: '+n.community),
      hoverinfo: 'text'
    }});
  }});

  const layout = {{
    showlegend: true, legend: {{ x:0, y:1, bgcolor:'rgba(30,41,59,0.9)', font:{{color:'#e2e8f0'}} }},
    xaxis: {{ showgrid:false, zeroline:false, showticklabels:false }},
    yaxis: {{ showgrid:false, zeroline:false, showticklabels:false }},
    plot_bgcolor: '#0f172a', paper_bgcolor: '#0f172a',
    font: {{ color: '#e2e8f0' }},
    hovermode: 'closest',
    margin: {{ l:10, r:10, t:10, b:10 }}
  }};

  Plotly.react('graph', traces, layout, {{ responsive: true }});
  document.getElementById('stat-line').textContent = filtered.length + ' nodes, ' + edgeCount + ' edges shown';
}}

applyFilters();
</script></body></html>"""


def build_figure(G: nx.Graph, pos: dict, title: str = "Knowledge Graph") -> go.Figure:
    """Build an interactive plotly figure from the subgraph."""
    if G.number_of_nodes() == 0:
        fig = go.Figure()
        fig.add_annotation(text="No nodes match your query.", showarrow=False, font=dict(size=20))
        return fig

    # Assign community colors
    communities = load_communities()
    node_community: dict[str, int] = {}
    for cid, comm_data in communities.get("communities", {}).items():
        for member in comm_data.get("members", []):
            node_community[member] = int(cid)

    # Edge traces (draw edges as lines)
    edge_x: list[float | None] = []
    edge_y: list[float | None] = []
    for u, v in G.edges():
        if u in pos and v in pos:
            x0, y0 = pos[u]
            x1, y1 = pos[v]
            edge_x.extend([x0, x1, None])
            edge_y.extend([y0, y1, None])

    edge_trace = go.Scatter(
        x=edge_x, y=edge_y,
        line=dict(width=0.3, color="#cbd5e1"),
        hoverinfo="none",
        mode="lines",
        showlegend=False,
    )

    # Node traces - group by type for legend
    traces = [edge_trace]

    for node_type, color in TYPE_COLORS.items():
        type_nodes = [n for n in G.nodes() if G.nodes[n].get("type") == node_type and n in pos]
        if not type_nodes:
            continue

        node_x = [pos[n][0] for n in type_nodes]
        node_y = [pos[n][1] for n in type_nodes]
        node_sizes = [max(8, min(40, G.degree(n) * 0.5 + 5)) for n in type_nodes]

        # Color by community if available
        node_colors = []
        for n in type_nodes:
            cid = node_community.get(n, 0)
            node_colors.append(COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)])

        hover_texts = []
        for n in type_nodes:
            data = G.nodes[n]
            label = data.get("label", n.split(":", 1)[-1])
            tags = ", ".join(data.get("tags", [])[:5])
            degree = G.degree(n)
            cid = node_community.get(n, -1)
            hover_texts.append(
                f"<b>{label}</b><br>"
                f"Type: {node_type}<br>"
                f"Connections: {degree}<br>"
                f"Tags: {tags}<br>"
                f"Community: {cid}"
            )

        node_labels = [G.nodes[n].get("label", n.split(":", 1)[-1]) for n in type_nodes]

        trace = go.Scatter(
            x=node_x, y=node_y,
            mode="markers+text",
            name=f"{node_type}s ({len(type_nodes)})",
            marker=dict(
                size=node_sizes,
                color=node_colors,
                line=dict(width=1, color="#1e293b"),
                opacity=0.85,
            ),
            text=node_labels,
            textposition="top center",
            textfont=dict(size=7, color="#475569"),
            hovertext=hover_texts,
            hoverinfo="text",
        )
        traces.append(trace)

    fig = go.Figure(data=traces)
    fig.update_layout(
        title=dict(text=title, font=dict(size=16)),
        showlegend=True,
        legend=dict(x=0, y=1, bgcolor="rgba(255,255,255,0.8)"),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor="#0f172a",
        paper_bgcolor="#0f172a",
        font=dict(color="#e2e8f0"),
        hovermode="closest",
        margin=dict(l=20, r=20, t=50, b=20),
        width=1400,
        height=900,
    )

    return fig


def get_available_tags(G: nx.Graph) -> list[tuple[str, int]]:
    """Get all tags with their node counts, sorted by frequency."""
    tag_counts: dict[str, int] = defaultdict(int)
    for _, data in G.nodes(data=True):
        for tag in data.get("tags", []):
            if tag != "uncategorized":
                tag_counts[tag] += 1
    return sorted(tag_counts.items(), key=lambda x: -x[1])


def interactive_menu(G: nx.Graph) -> None:
    """Interactive filtering menu when no CLI args provided."""
    skills = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "skill")
    agents = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "agent")
    communities = load_communities()
    num_communities = len(communities.get("communities", {}))
    tags = get_available_tags(G)

    print(f"""
=== Knowledge Graph Visualizer ===

Full graph: {G.number_of_nodes()} nodes ({skills} skills, {agents} agents), {G.number_of_edges()} edges
Communities: {num_communities} | Tags: {len(tags)}

The full graph is too large to render. Choose a view:

  [1] Neighborhood  - Explore around a specific skill/agent
  [2] Tag filter    - Show skills/agents with a specific tag
  [3] Community     - Show a detected community cluster
  [4] Top connected - Show the N most connected nodes
  [5] Stats only    - Print graph statistics
  [Q] Quit
""")

    choice = input("Choose [1-5, Q]: ").strip()

    seeds = None
    hops = 1
    min_weight = 1
    community_id = None
    tag_filter = None
    top_n = None
    output = None

    if choice == "1":
        print("\nAvailable example seeds: fastapi-pro, docker-expert, python-pro, exploitation-validator")
        seed_input = input("Enter skill/agent name(s) (comma-separated): ").strip()
        if not seed_input:
            print("No seed provided.")
            return
        seeds = [s.strip() for s in seed_input.split(",")]
        hops_input = input("Hop depth [1]: ").strip()
        hops = int(hops_input) if hops_input else 1
        weight_input = input("Min edge weight [1]: ").strip()
        min_weight = int(weight_input) if weight_input else 1

    elif choice == "2":
        print(f"\nTop tags: {', '.join(f'{t}({c})' for t, c in tags[:20])}")
        tag_filter = input("Enter tag: ").strip()
        if not tag_filter:
            print("No tag provided.")
            return
        top_input = input("Max nodes to show [30]: ").strip()
        top_n = int(top_input) if top_input else 30

    elif choice == "3":
        top_comms = sorted(
            communities.get("communities", {}).items(),
            key=lambda x: -len(x[1].get("members", [])),
        )[:10]
        print("\nTop 10 communities:")
        for cid, cdata in top_comms:
            print(f"  [{cid}] {cdata.get('label', '?')} ({len(cdata.get('members', []))} members)")
        cid_input = input("Enter community ID: ").strip()
        if not cid_input:
            print("No community ID provided.")
            return
        community_id = int(cid_input)

    elif choice == "4":
        top_input = input("How many top nodes? [50]: ").strip()
        top_n = int(top_input) if top_input else 50
        weight_input = input("Min edge weight [2]: ").strip()
        min_weight = int(weight_input) if weight_input else 2

    elif choice == "5":
        print(f"\nFull graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
        print(f"  Skills: {skills}, Agents: {agents}")
        top = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)[:10]
        print(f"\nTop 10 by connections:")
        for n in top:
            print(f"  {G.nodes[n].get('label', n)} ({G.degree(n)} connections)")
        print(f"\nTop tags: {', '.join(f'{t}({c})' for t, c in tags[:15])}")
        print(f"Communities: {num_communities}")
        return

    elif choice.upper() == "Q":
        return

    else:
        print("Invalid choice.")
        return

    save_input = input("Save to HTML file? (enter path or press Enter to open browser): ").strip()
    if save_input:
        output = save_input

    sub = extract_subgraph(
        G,
        seeds=seeds,
        hops=hops,
        min_weight=min_weight,
        community_id=community_id,
        tag_filter=tag_filter,
        top_n=top_n,
    )

    print(f"Subgraph: {sub.number_of_nodes()} nodes, {sub.number_of_edges()} edges")

    if sub.number_of_nodes() == 0:
        print("No nodes matched your query.")
        return

    if sub.number_of_nodes() > 500:
        print(f"Warning: {sub.number_of_nodes()} nodes is large. Rendering may be slow.")

    parts = []
    if seeds:
        parts.append(f"from {', '.join(seeds)}")
    if community_id is not None:
        parts.append(f"community {community_id}")
    if tag_filter:
        parts.append(f"tag: {tag_filter}")
    if top_n:
        parts.append(f"top {top_n}")
    title = f"Knowledge Graph ({sub.number_of_nodes()} nodes) - {' | '.join(parts)}"

    pos = compute_layout(sub)
    fig = build_figure(sub, pos, title=title)

    if output:
        fig.write_html(output, include_plotlyjs=True)
        print(f"Saved to {output}")
    else:
        fig.show()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive knowledge graph visualization",
        epilog="The full graph is too large to render. Use boundaries to focus on a region.",
    )
    parser.add_argument("--seed", help="Comma-separated skill/agent names to center on")
    parser.add_argument("--hops", type=int, default=1, help="Neighborhood depth from seeds (default 1)")
    parser.add_argument("--min-weight", type=int, default=1, help="Minimum edge weight to include (default 1)")
    parser.add_argument("--community", type=int, help="Show a specific community by ID")
    parser.add_argument("--tag", help="Show only nodes with this tag")
    parser.add_argument("--top", type=int, help="Show only the top-N most connected nodes")
    parser.add_argument("--output", help="Save to HTML file instead of opening browser")
    parser.add_argument("--stats", action="store_true", help="Print graph stats and exit")
    args = parser.parse_args()

    G = load_graph()

    if args.stats:
        print(f"Full graph: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")
        skills = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "skill")
        agents = sum(1 for _, d in G.nodes(data=True) if d.get("type") == "agent")
        print(f"  Skills: {skills}, Agents: {agents}")
        top = sorted(G.nodes(), key=lambda n: G.degree(n), reverse=True)[:10]
        print(f"\nTop 10 by connections:")
        for n in top:
            print(f"  {G.nodes[n].get('label', n)} ({G.degree(n)} connections)")
        communities = load_communities()
        print(f"\nCommunities: {len(communities.get('communities', {}))}")
        return

    if not (args.seed or args.community is not None or args.tag or args.top):
        interactive_menu(G)
        return

    # Parse seeds
    seeds = [s.strip() for s in args.seed.split(",")] if args.seed else None

    # Extract subgraph
    sub = extract_subgraph(
        G,
        seeds=seeds,
        hops=args.hops,
        min_weight=args.min_weight,
        community_id=args.community,
        tag_filter=args.tag,
        top_n=args.top,
    )

    print(f"Subgraph: {sub.number_of_nodes()} nodes, {sub.number_of_edges()} edges")

    if sub.number_of_nodes() > 500:
        print(f"Warning: {sub.number_of_nodes()} nodes is large. Consider tighter boundaries.", file=sys.stderr)
        print("  Add --top 100 or --min-weight 3 to reduce.", file=sys.stderr)

    # Build title
    parts = []
    if seeds:
        parts.append(f"from {', '.join(seeds)}")
    if args.community is not None:
        parts.append(f"community {args.community}")
    if args.tag:
        parts.append(f"tag: {args.tag}")
    if args.top:
        parts.append(f"top {args.top}")
    title = f"Knowledge Graph ({sub.number_of_nodes()} nodes) - {' | '.join(parts)}"

    # Layout + render with embedded filter sidebar
    pos = compute_layout(sub)
    output_path = args.output or "graph-view.html"
    html = build_html_with_filters(sub, pos, title=title)
    Path(output_path).write_text(html, encoding="utf-8")
    print(f"Saved to {output_path}")

    if not args.output:
        import webbrowser
        webbrowser.open(str(Path(output_path).resolve()))


if __name__ == "__main__":
    main()
