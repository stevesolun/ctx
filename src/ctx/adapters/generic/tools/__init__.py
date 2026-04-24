"""ctx.adapters.generic.tools — MCP-first tool routing.

MCP has emerged as the cross-harness tool protocol (Cline, Goose, Claude
Agent SDK, OpenHands all speak it). We spawn MCP servers as subprocesses,
maintain stdio streams, and route model tool-calls to them.

Populated in phase H2.
"""
