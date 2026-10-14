# Your catalog

CTX ships the catalog infrastructure, not the catalog contents. A fresh
installation contains **zero bundled skills, agents, MCP servers, or harnesses**.
You and your organization choose which records to author or import.

The tiles below are **schema and search examples**, not installed tools or a
public inventory. Their filters illustrate the supported entity types and
possible queries. Your actual records appear only in your local catalog:

```bash
ctx-init --model-mode skip
python -m ctx_monitor serve
```

Start with [entity onboarding](entity-onboarding.md), then
[build your knowledge graph](knowledge-graph.md#rebuilding).
A description or installation command is metadata, not approval to execute it.
The optional `ctx fit` evaluator is separate from catalog browsing.

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

  <div id="ctx-catalog-grid" class="ctx-catalog-grid">
    <article class="ctx-catalog-card" data-type="skill" data-search="skill prompt workflow testing code review frontend backend security research">
      <span class="ctx-catalog-pill">Skills</span>
      <h3>Skills</h3>
      <p class="ctx-catalog-muted">0 bundled entities; add your own records</p>
      <a class="md-button" href="./?type=skill">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="agent" data-search="agent reviewer planner architect debugger security research">
      <span class="ctx-catalog-pill">Agents</span>
      <h3>Agents</h3>
      <p class="ctx-catalog-muted">0 bundled entities; add your own records</p>
      <a class="md-button" href="./?type=agent">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="mcp-server" data-search="mcp server github filesystem browser database api cloud">
      <span class="ctx-catalog-pill">MCPs</span>
      <h3>MCP servers</h3>
      <p class="ctx-catalog-muted">0 bundled entities; add your own records</p>
      <a class="md-button" href="./?type=mcp-server">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="harness" data-search="harness local model api model llm orchestration verification">
      <span class="ctx-catalog-pill">Harnesses</span>
      <h3>Harnesses</h3>
      <p class="ctx-catalog-muted">0 bundled entities; add your own records</p>
      <a class="md-button" href="./?type=harness">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="skill" data-search="code review review pr diff quality bug tests">
      <span class="ctx-catalog-pill">Skills</span>
      <h3>Code review skills</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=skill&q=code+review">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="skill" data-search="testing pytest unit browser smoke regression">
      <span class="ctx-catalog-pill">Skills</span>
      <h3>Testing skills</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=skill&q=testing">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="skill" data-search="frontend ui dashboard css react browser">
      <span class="ctx-catalog-pill">Skills</span>
      <h3>Frontend skills</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=skill&q=frontend">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="agent" data-search="architecture design refactor planning">
      <span class="ctx-catalog-pill">Agents</span>
      <h3>Architecture agents</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=agent&q=architecture">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="agent" data-search="security audit supply chain secrets">
      <span class="ctx-catalog-pill">Agents</span>
      <h3>Security agents</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=agent&q=security">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="mcp-server" data-search="github repo issues pull requests graphql">
      <span class="ctx-catalog-pill">MCPs</span>
      <h3>GitHub MCPs</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=mcp-server&q=github">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="mcp-server" data-search="cloud google cloud aws azure deploy">
      <span class="ctx-catalog-pill">MCPs</span>
      <h3>Cloud MCPs</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=mcp-server&q=cloud">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="mcp-server" data-search="browser automation web scraping">
      <span class="ctx-catalog-pill">MCPs</span>
      <h3>Browser MCPs</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=mcp-server&q=browser">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="harness" data-search="local api openai ollama vllm model harness">
      <span class="ctx-catalog-pill">Harnesses</span>
      <h3>Local/API model harnesses</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=harness&q=local+model">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="harness" data-search="harness test eval guardrail validate verification">
      <span class="ctx-catalog-pill">Harnesses</span>
      <h3>Verification harnesses</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=harness&q=verification">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
    <article class="ctx-catalog-card" data-type="harness" data-search="harness tools sandbox filesystem cloud tool access">
      <span class="ctx-catalog-pill">Harnesses</span>
      <h3>Tool-access harnesses</h3>
      <p class="ctx-catalog-muted">Example search; no bundled records</p>
      <a class="md-button" href="./?type=harness&q=tool+access">Filter tiles</a>
      <a class="md-button" href="../dashboard/#catalog-badge-links">Open your catalog locally</a>
    </article>
  </div>
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
.ctx-catalog-card[hidden] {
  display: none;
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
