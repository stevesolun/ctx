---
name: sandboxed-tool-runtime
description: Agent tool execution should happen inside a disposable container, not the host — isolation by default, not opt-in
source: Distilled from Strix (https://github.com/usestrix/strix, Apache-2.0) — rev 15c95718
category: agent-architecture
---

# Sandboxed Tool Runtime

When an LLM agent can run shell commands, HTTP requests, file operations, or code, the blast radius of a bad decision is unbounded. The sandboxed-runtime pattern constrains that blast radius by running every agent-initiated operation inside an ephemeral container.

## Shape of the Pattern

- One Docker (or equivalent) container per scan/session.
- Container has the toolchain pre-installed (scanners, browsers, language runtimes) and no credentials except what the host explicitly injects.
- Agent tool calls are serialized and sent to a tool-server process running *inside* the container.
- Target source code or artifacts are mounted at `/workspace/...` inside the container, not bind-mounted read-write to host-sensitive paths.
- Workspace subdirectories are created per-target so multiple targets in one run don't collide.
- Container lifetime == session lifetime. Cleanup on exit.

## Trust Boundaries

- **Host** trusts the agent to ask for operations but not to execute them directly. All operations go through the container.
- **Container** is untrusted by the host — its filesystem, network, and exit code are the only channels back.
- **Agent** is untrusted by the container's outer security policy — it can only call the registered tool actions, not arbitrary binaries (though inside the container shell access is typically granted, it's still a container, so the explosion radius is one container).

## Why This Beats Running Tools on the Host

- LLM agents produce unpredictable command strings. If even 1 in 10,000 is destructive (rm of wrong path, overly-broad git reset), running on host is catastrophic.
- Tooling dependencies (nmap, nuclei, specific Python versions, Playwright browsers) don't pollute the host.
- Reruns are deterministic — start a fresh container, get the same initial state.
- Credentials can be scoped to the session: inject `TOKEN=...` as container env, it's gone when the container exits.

## Key Design Choices

- **Non-interactive mode support** — the container runs long-lived tool-server; the agent talks to it via a local socket/HTTP boundary.
- **Streaming output** — tool-server streams stdout/stderr back as it arrives, not in a single blob at the end.
- **Cancellation** — the agent must be able to cancel a running tool (kill the process tree inside the container) without tearing down the whole session.
- **Pre-pulled images** — first run pulls the sandbox image (often hundreds of MB). Subsequent runs are fast because the image is cached.

## When This Is Overkill

- Pure LLM workloads with no tool calls — no blast radius to contain.
- Single-file text transforms where the agent rewrites and the host validates after — the host itself is the sandbox in effect.
- Short, well-typed tool sets (e.g. "this agent can only call `search_docs` and `read_file`") where each tool is individually safe.

## When This Is Required

- Agent executes arbitrary shell or code.
- Agent performs network operations against external targets.
- Agent modifies files in arbitrary paths.
- Multiple agent runs need reproducible, isolated environments.
