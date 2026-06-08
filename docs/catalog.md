# Catalog

Use this page when you click a README badge from GitHub, PyPI, or Hugging Face.
It is public and always reachable. The full live catalog runs locally inside
`ctx-monitor`.

!!! tip "Run the full local catalog"
    ```bash
    ctx-init --graph --graph-install-mode full --model-mode skip
    ctx-monitor serve
    ```

<div class="ctx-catalog-app">
  <div class="ctx-catalog-search">
    <label for="ctx-catalog-query"><strong>Search catalog</strong></label>
    <input id="ctx-catalog-query" list="ctx-catalog-suggestions" placeholder="github, code review, google cloud, testing..." />
    <datalist id="ctx-catalog-suggestions">
      <option value="github"></option>
      <option value="code review"></option>
      <option value="google cloud"></option>
      <option value="testing"></option>
      <option value="security"></option>
      <option value="frontend"></option>
      <option value="agent harness"></option>
      <option value="local model"></option>
      <option value="mcp server"></option>
      <option value="research"></option>
      <option value="book to skill"></option>
      <option value="browser automation"></option>
    </datalist>
    <div class="ctx-catalog-filters" role="group" aria-label="Entity type filters">
      <label><input type="checkbox" value="skill" checked /> Skills</label>
      <label><input type="checkbox" value="agent" checked /> Agents</label>
      <label><input type="checkbox" value="mcp-server" checked /> MCPs</label>
      <label><input type="checkbox" value="harness" checked /> Harnesses</label>
    </div>
  </div>

  <div class="ctx-catalog-actions">
    <a class="md-button md-button--primary" href="../dashboard/#catalog-badge-links">Local catalog setup</a>
    <a class="md-button" href="../knowledge-graph/">Knowledge graph docs</a>
  </div>

  <p id="ctx-catalog-count" class="ctx-catalog-muted"></p>

  <div id="ctx-catalog-grid" class="ctx-catalog-grid"></div>
</div>

<style>
.ctx-catalog-app {
  display: grid;
  gap: 1rem;
}
.ctx-catalog-search {
  border: 1px solid var(--md-default-fg-color--lightest);
  border-radius: 10px;
  padding: 1rem;
  background: var(--md-default-bg-color);
}
.ctx-catalog-search input[type="text"], #ctx-catalog-query {
  width: 100%;
  box-sizing: border-box;
  margin-top: 0.35rem;
  padding: 0.65rem 0.75rem;
  border: 1px solid var(--md-default-fg-color--lighter);
  border-radius: 8px;
  font: inherit;
}
.ctx-catalog-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-top: 0.75rem;
}
.ctx-catalog-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.65rem;
}
.ctx-catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
  gap: 0.9rem;
}
.ctx-catalog-card {
  border: 1px solid var(--md-default-fg-color--lightest);
  border-radius: 10px;
  padding: 1rem;
  display: grid;
  gap: 0.55rem;
  background: var(--md-default-bg-color);
  box-shadow: 0 1px 8px rgba(0, 0, 0, 0.04);
}
.ctx-catalog-card h3 {
  margin: 0;
}
.ctx-catalog-pill {
  width: fit-content;
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  font-size: 0.72rem;
  background: var(--md-accent-fg-color--transparent);
}
.ctx-catalog-muted {
  color: var(--md-default-fg-color--light);
}
.ctx-catalog-card a {
  width: fit-content;
}
</style>

<script>
const ctxCatalogItems = [
  {type: "skill", title: "Skills", count: "91,464", query: "", tags: "skill prompt workflow testing code review frontend backend security research"},
  {type: "agent", title: "Agents", count: "467", query: "", tags: "agent reviewer planner architect debugger security research"},
  {type: "mcp-server", title: "MCP servers", count: "10,790", query: "", tags: "mcp server github filesystem browser database api cloud"},
  {type: "harness", title: "Harnesses", count: "207", query: "", tags: "harness local model api model llm orchestration verification"},
  {type: "skill", title: "Code review skills", count: "search", query: "code review", tags: "review pr diff quality bug tests"},
  {type: "skill", title: "Testing skills", count: "search", query: "testing", tags: "pytest unit browser smoke regression"},
  {type: "skill", title: "Frontend skills", count: "search", query: "frontend", tags: "ui dashboard css react browser"},
  {type: "agent", title: "Architecture agents", count: "search", query: "architecture", tags: "architecture design refactor planning"},
  {type: "agent", title: "Security agents", count: "search", query: "security", tags: "security audit supply chain secrets"},
  {type: "mcp-server", title: "GitHub MCPs", count: "search", query: "github", tags: "github repo issues pull requests graphql"},
  {type: "mcp-server", title: "Cloud MCPs", count: "search", query: "cloud", tags: "google cloud aws azure deploy"},
  {type: "mcp-server", title: "Browser MCPs", count: "search", query: "browser", tags: "browser automation web scraping"},
  {type: "harness", title: "Local/API model harnesses", count: "search", query: "local model", tags: "local api openai ollama vllm model harness"},
  {type: "harness", title: "Verification harnesses", count: "search", query: "verification", tags: "harness test eval guardrail validate"},
  {type: "harness", title: "Tool-access harnesses", count: "search", query: "tool access", tags: "harness tools sandbox filesystem cloud"},
];

const ctxTypeLabels = {
  "skill": "Skills",
  "agent": "Agents",
  "mcp-server": "MCPs",
  "harness": "Harnesses",
};

function ctxParam(name) {
  return new URLSearchParams(window.location.search).get(name) || "";
}

function ctxSelectedTypes() {
  return Array.from(document.querySelectorAll(".ctx-catalog-filters input:checked")).map((el) => el.value);
}

function ctxPublicCatalogUrl(type, query) {
  const params = new URLSearchParams();
  if (type) params.set("type", type);
  if (query) params.set("q", query);
  const suffix = params.toString();
  return "./" + (suffix ? "?" + suffix : "");
}

function ctxRenderCatalog() {
  const query = document.getElementById("ctx-catalog-query").value.trim().toLowerCase();
  const selected = new Set(ctxSelectedTypes());
  const grid = document.getElementById("ctx-catalog-grid");
  const items = ctxCatalogItems.filter((item) => {
    const hay = `${item.type} ${item.title} ${item.query} ${item.tags}`.toLowerCase();
    return selected.has(item.type) && (!query || hay.includes(query));
  });
  grid.innerHTML = items.map((item) => {
    const launchQuery = query || item.query;
    const href = ctxPublicCatalogUrl(item.type, launchQuery);
    return `<article class="ctx-catalog-card">
      <span class="ctx-catalog-pill">${ctxTypeLabels[item.type]}</span>
      <h3>${item.title}</h3>
      <p class="ctx-catalog-muted">${item.count === "search" ? "Filtered catalog launcher" : item.count + " entities"}</p>
      <a class="md-button" href="${href}">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open full catalog locally</a>
    </article>`;
  }).join("");
  document.getElementById("ctx-catalog-count").textContent = `${items.length} tiles shown`;
}

const initialType = ctxParam("type");
const initialQuery = ctxParam("q");
if (initialQuery) document.getElementById("ctx-catalog-query").value = initialQuery;
if (initialType) {
  document.querySelectorAll(".ctx-catalog-filters input").forEach((el) => {
    el.checked = el.value === initialType;
  });
}
document.getElementById("ctx-catalog-query").addEventListener("input", ctxRenderCatalog);
document.querySelectorAll(".ctx-catalog-filters input").forEach((el) => el.addEventListener("change", ctxRenderCatalog));
ctxRenderCatalog();
</script>
