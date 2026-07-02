"""Harness Setup page renderer for ctx-monitor."""

from __future__ import annotations

import html
from typing import Any


def render_harness_wizard(
    *,
    harnesses: list[dict[str, Any]],
    layout,
) -> str:
    """Render the manual harness setup wizard for user-owned LLMs."""
    provider_options = (
        "openai",
        "anthropic",
        "google",
        "huggingface",
        "openrouter",
        "ollama",
        "lm-studio",
        "local",
        "other",
    )
    provider_html = "".join(
        f"<option value='{html.escape(provider)}'>{html.escape(provider)}</option>"
        for provider in provider_options
    )
    tool_options = (
        ("files", "Files"),
        ("git", "Git"),
        ("shell", "Shell"),
        ("browser", "Browser"),
        ("http", "HTTP/network"),
        ("package-manager", "Package manager"),
        ("database", "Database"),
    )
    tools_html = "".join(
        "<label style='display:flex; align-items:center; gap:0.35rem;'>"
        f"<input type='checkbox' name='tools' value='{html.escape(value)}' "
        f"{'checked' if value in {'files', 'git', 'shell'} else ''}>"
        f"{html.escape(label)}</label>"
        for value, label in tool_options
    )
    harness_cards = "".join(
        "<div class='harness-card' "
        f"data-harness-slug='{html.escape(row['slug'])}' "
        f"data-harness-text='{html.escape(' '.join([row['slug'], row['title'], row['description'], *row['tags']]).lower())}' "
        f"data-harness-score='{float(row['score']):.3f}'>"
        "<div style='display:flex; justify-content:space-between; gap:0.5rem; align-items:start;'>"
        f"<strong>{html.escape(row['title'])}</strong>"
        + (
            f"<span class='pill grade-{html.escape(row['grade'])}'>{html.escape(row['grade'])}</span>"
            if row["grade"]
            else "<span class='pill entity-type-harness'>harness</span>"
        )
        + "</div>"
        f"<p class='muted' style='margin:0;'>{html.escape(row['description'] or 'No description available.')}</p>"
        + (
            "<div class='muted' style='font-size:0.78rem;'>"
            + " ".join(f"<code>{html.escape(tag)}</code>" for tag in row["tags"][:8])
            + "</div>"
            if row["tags"]
            else ""
        )
        + (
            f"<a class='muted' href='{html.escape(row['repo_url'])}'>{html.escape(row['repo_url'])}</a>"
            if row["repo_url"].startswith(("http://", "https://"))
            else ""
        )
        + f"<code>ctx-harness-install {html.escape(row['slug'])} --dry-run</code>"
        + f"<button type='button' class='secondary' data-select-harness='{html.escape(row['slug'])}'>select</button>"
        + "</div>"
        for row in harnesses
    )
    if not harness_cards:
        harness_cards = (
            "<p class='muted'>No harness pages were found under "
            "<code>~/.claude/skill-wiki/entities/harnesses/</code>. "
            "Use the no-fit PRD output below to build an attachable harness.</p>"
        )

    body = (
        "<div class='setup-header'>"
        "<div><div class='setup-kicker'>Model -> intent -> install -> attach ctx</div>"
        "<h1>Harness Setup</h1>"
        "<p class='muted'>For users running their own API or local model instead of Claude Code. "
        "Interview the model/runtime choice, generate a real ctx harness recommendation command, "
        "then install a harness or produce a no-fit PRD for a custom harness.</p></div>"
        "<span class='pill entity-type-harness'>local/API model path</span>"
        "</div>"
        "<div class='setup-flow'>"
        "<div class='setup-flow-step'><strong>1. Model</strong><span class='muted'>Provider, model slug, endpoint.</span></div>"
        "<div class='setup-flow-step'><strong>2. Intent</strong><span class='muted'>Goal, OS, access, privacy.</span></div>"
        "<div class='setup-flow-step'><strong>3. Install</strong><span class='muted'>Recommend, dry-run, install.</span></div>"
        "<div class='setup-flow-step'><strong>4. Attach ctx</strong><span class='muted'>Graph/wiki recommendations flow into the harness.</span></div>"
        "</div>"
        "<div class='wizard-layout'>"
        "<form id='harness-wizard-form' class='card'>"
        "<div class='wizard-step'><strong>1. Model</strong>"
        "<div class='wizard-grid' style='margin-top:0.65rem;'>"
        "<label>Model provider <span class='pill grade-A'>Required</span>"
        f"<select name='model_provider' required>{provider_html}</select></label>"
        "<label>Model slug <span class='pill grade-A'>Required</span>"
        "<input name='model' required placeholder='openai/gpt-5.5 or ollama/qwen3-coder'></label>"
        "<label class='wide'>API base URL or local endpoint"
        "<input name='endpoint' placeholder='https://api.openai.com/v1 or http://localhost:11434'></label>"
        "</div></div>"
        "<div class='wizard-step'><strong>2. Goal and access</strong>"
        "<div class='wizard-grid' style='margin-top:0.65rem;'>"
        "<label class='wide'>Development goal <span class='pill grade-A'>Required</span>"
        "<textarea name='goal' rows='4' required placeholder='What should the agent build, fix, research, or operate?'></textarea></label>"
        "<label>Runtime / OS"
        "<select name='runtime'><option>windows</option><option>macos</option><option>linux</option>"
        "<option selected>cross-platform</option></select></label>"
        "<label>Autonomy"
        "<select name='autonomy'><option>read-only</option><option selected>repo-write</option>"
        "<option>deploy-capable</option></select></label>"
        "<label class='wide'>Allowed tools"
        f"<div style='display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:0.25rem;'>{tools_html}</div></label>"
        "<label>Verification gates"
        "<input name='verify' placeholder='pytest, ruff, mypy, build, smoke'></label>"
        "<label>Privacy / network"
        "<select name='privacy'><option selected>local repo only</option><option>network allowed</option>"
        "<option>secrets allowed by env only</option><option>offline only</option></select></label>"
        "<label>ctx attachment"
        "<select name='attach_mode'><option selected>mcp</option><option>python</option><option>cli</option></select></label>"
        "</div></div>"
        "<div class='wizard-step'><strong>3. Recommend and install</strong>"
        "<p class='muted'>The dashboard previews catalog matches. The command below calls the real harness recommender and keeps the no-fit path available.</p>"
        "<button type='submit'>build recommendation command</button> "
        "<button type='button' id='harness-reset' class='secondary'>reset</button>"
        "</div>"
        "</form>"
        "<aside class='card'>"
        "<h2 style='margin-top:0;'>Command plan</h2>"
        "<pre class='command-box' data-testid='harness-command-output'>ctx-harness-install --recommend --goal \"...\" --model-provider openai --model openai/gpt-5.5 --top-k 5 --plan-on-no-fit</pre>"
        "<p class='muted'>Run the dry-run first. The installer writes attach files under the harness target so the selected harness can connect to ctx graph/wiki recommendations.</p>"
        "<div id='selected-harness-command' class='muted'>Select a harness card to see install, update, and validation commands.</div>"
        "</aside>"
        "</div>"
        "<section class='card'>"
        "<div style='display:flex; justify-content:space-between; gap:0.75rem; align-items:center; flex-wrap:wrap;'>"
        "<div><h2 style='margin:0;'>Catalog harnesses</h2>"
        "<p class='muted' style='margin:0.2rem 0 0;'>Cards are filtered by the interview text. If none fit, use the no-fit PRD path.</p></div>"
        "<span id='harness-match-count' class='pill entity-type-harness'>0 matches</span>"
        "</div>"
        "<div id='harness-cards' style='display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:0.7rem; margin-top:0.8rem;'>"
        + harness_cards
        + "</div>"
        "</section>"
        "<section class='card'>"
        "<h2>No-fit custom harness PRD</h2>"
        "<p class='muted'>When no catalog harness clears the configured match score, generate a PRD for the user's strong model or engineering team. It must include orchestration, durable state, permissions, verification gates, and ctx recommendation hooks.</p>"
        "<pre class='command-box' id='no-fit-command'>ctx-harness-install --recommend --goal \"...\" --model-provider openai --model openai/gpt-5.5 --plan-on-no-fit --plan-output custom-harness-prd.md</pre>"
        "</section>"
        "<script>\n"
        "(function () {\n"
        "  const form = document.getElementById('harness-wizard-form');\n"
        "  const output = document.querySelector('[data-testid=\"harness-command-output\"]');\n"
        "  const noFit = document.getElementById('no-fit-command');\n"
        "  const selected = document.getElementById('selected-harness-command');\n"
        "  const count = document.getElementById('harness-match-count');\n"
        "  const cards = Array.from(document.querySelectorAll('.harness-card'));\n"
        "  function value(name) { const el = form.elements[name]; return el ? String(el.value || '').trim() : ''; }\n"
        "  function shellQuote(value) { return '\"' + String(value || '').replace(/\\\\/g, '\\\\\\\\').replace(/\"/g, '\\\\\"') + '\"'; }\n"
        "  function checkedTools() { return Array.from(form.querySelectorAll('input[name=\"tools\"]:checked')).map(el => el.value).join(','); }\n"
        "  function arg(flag, val) { return val ? ' ' + flag + ' ' + shellQuote(val) : ''; }\n"
        "  function recommendCommand() {\n"
        "    const tools = checkedTools();\n"
        "    let cmd = 'ctx-harness-install --recommend';\n"
        "    cmd += arg('--goal', value('goal'));\n"
        "    cmd += arg('--model-provider', value('model_provider'));\n"
        "    cmd += arg('--model', value('model'));\n"
        "    cmd += arg('--harness-runtime', value('runtime'));\n"
        "    cmd += arg('--harness-autonomy', value('autonomy'));\n"
        "    cmd += arg('--harness-tools', tools);\n"
        "    cmd += arg('--harness-verify', value('verify'));\n"
        "    cmd += arg('--harness-privacy', value('privacy'));\n"
        "    cmd += arg('--harness-attach-mode', value('attach_mode'));\n"
        "    return cmd + ' --top-k 5 --plan-on-no-fit';\n"
        "  }\n"
        "  function fitCards() {\n"
        "    const intent = [value('goal'), value('model_provider'), value('model'), value('runtime'), value('autonomy'), checkedTools(), value('verify'), value('privacy'), value('attach_mode')].join(' ').toLowerCase();\n"
        "    const terms = intent.split(/[^a-z0-9_.-]+/).filter(Boolean);\n"
        "    const host = document.getElementById('harness-cards');\n"
        "    let visible = 0;\n"
        "    cards.forEach(card => {\n"
        "      const text = card.dataset.harnessText || '';\n"
        "      const base = Number(card.dataset.harnessScore || 0);\n"
        "      const hits = terms.filter(term => text.includes(term)).length;\n"
        "      const fit = base + hits * 0.08;\n"
        "      card.dataset.fit = fit.toFixed(3);\n"
        "      const hide = terms.length > 0 && fit < 0.12;\n"
        "      card.dataset.fitHidden = hide ? 'true' : 'false';\n"
        "      if (!hide) visible++;\n"
        "    });\n"
        "    cards.sort((a, b) => Number(b.dataset.fit || 0) - Number(a.dataset.fit || 0)).forEach(card => host.appendChild(card));\n"
        "    count.textContent = visible + ' matches';\n"
        "  }\n"
        "  function refresh() {\n"
        "    const cmd = recommendCommand();\n"
        "    output.textContent = cmd;\n"
        "    noFit.textContent = cmd + ' --plan-output custom-harness-prd.md';\n"
        "    fitCards();\n"
        "  }\n"
        "  form.addEventListener('submit', ev => { ev.preventDefault(); refresh(); });\n"
        "  form.addEventListener('input', refresh);\n"
        "  document.getElementById('harness-reset').addEventListener('click', () => { form.reset(); refresh(); });\n"
        "  document.querySelectorAll('[data-select-harness]').forEach(btn => btn.addEventListener('click', () => {\n"
        "    const slug = btn.dataset.selectHarness || '';\n"
        "    cards.forEach(card => card.classList.toggle('selected', card.dataset.harnessSlug === slug));\n"
        "    selected.innerHTML = '<pre class=\"command-box\">ctx-harness-install ' + slug + ' --dry-run\\nctx-harness-install ' + slug + '\\nctx-harness-install ' + slug + ' --update --dry-run\\nctx-scan-repo --repo . --recommend\\nctx-monitor serve</pre>';\n"
        "  }));\n"
        "  refresh();\n"
        "})();\n"
        "</script>"
    )
    return layout("Harness Setup", body)
