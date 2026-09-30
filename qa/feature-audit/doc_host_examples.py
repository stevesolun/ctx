"""Run host-facing recommendation examples locally without a host or provider."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import networkx as nx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
import ctx.api
import ctx_config
import ctx.telemetry as telemetry
from ctx import CtxCoreToolbox
from ctx.adapters import loopflow

results = []
def blocks(path, language):
    return [chunk.split('```', 1)[0] for chunk in path.read_text().split('```' + language + '\n')[1:]]

with tempfile.TemporaryDirectory(prefix='ctx-doc-host-') as temp, contextlib.ExitStack() as stack:
    root = Path(temp)
    wiki = root / 'wiki'
    graph_path = wiki / 'graphify-out' / 'graph.json'
    graph_path.parent.mkdir(parents=True)
    graph = nx.Graph()
    fixtures = [('skill', 'fastapi-pro'), ('agent', 'code-reviewer'), ('mcp-server', 'local-ollama-file-operations'), ('mcp-server', 'filesystem'), ('harness', 'local-harness')]
    for kind, slug in fixtures:
        graph.add_node(f'{kind}:{slug}', label=slug, type=kind, tags=['fastapi', 'python', 'local', 'ollama', 'filesystem', 'agent', 'loop', 'mcp'], model_providers=['ollama'])
        folder = {'skill': 'skills', 'agent': 'agents', 'mcp-server': 'mcp-servers', 'harness': 'harnesses'}[kind]
        path = wiki / 'entities' / folder / (slug[0] if kind == 'mcp-server' else '') / f'{slug}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\ntitle: {slug}\ntype: {kind}\ndescription: Local Python FastAPI filesystem agent loop.\ntags: [fastapi, python, local, ollama, filesystem, agent, loop, mcp]\nmodel_providers: [ollama]\n---\n# {slug}\n\nUse local Python FastAPI filesystem tools.\n')
        if kind == 'skill':
            converted = wiki / 'converted' / slug / 'SKILL.md'
            converted.parent.mkdir(parents=True)
            converted.write_text(path.read_text())
    graph.add_edge('skill:fastapi-pro', 'agent:code-reviewer', weight=1.0)
    graph.add_edge('skill:fastapi-pro', 'mcp-server:filesystem', weight=0.9)
    graph_path.write_text(json.dumps(nx.node_link_data(graph, edges='edges')))
    for name, value in [('wiki_dir', wiki), ('claude_dir', root), ('graph_semantic_cache_dir', root / 'semantic-cache')]:
        stack.enter_context(patch.object(ctx_config.cfg, name, value))
    original_get = telemetry._config_get
    stack.enter_context(patch.object(telemetry, '_config_get', lambda key, default: {'enabled': False, 'mode': 'off', 'privacy': {'hash_salt_path': str(root / 'salt')}} if key == 'telemetry' else original_get(key, default)))
    stack.enter_context(patch.object(ctx.api, '_default_toolbox', CtxCoreToolbox(wiki_dir=wiki, graph_path=graph_path)))

    attach = ROOT / 'docs/harness/attaching-to-hosts.md'
    library = next(code for code in blocks(attach, 'python') if 'def on_user_turn' in code)
    injected = []
    namespace = {'inject_into_context': injected.append}
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exec(compile(library, str(attach), 'exec'), namespace)
        namespace['on_user_turn']('fastapi python async code review')
    assert injected and 'fastapi-pro' in injected[0]
    results.append({'example': 'exact Python on_user_turn example with host injection callback supplied', 'stdout': stdout.getvalue(), 'injected_body_count': len(injected), 'result': 'passed'})
    advanced = next(code for code in blocks(attach, 'python') if 'toolbox = CtxCoreToolbox' in code)
    advanced = advanced.replace('/path/to/custom/wiki', str(wiki)).replace('/path/to/custom/graph.json', str(graph_path))
    with contextlib.redirect_stdout(io.StringIO()) as output:
        exec(compile(advanced, str(attach), 'exec'), {})
    assert 'ctx__wiki_get' in output.getvalue()
    results.append({'example': 'custom-path toolbox catalog example with fixture path substitution', 'stdout': output.getvalue(), 'result': 'passed'})

    loop_file = root / 'select-capabilities.loop'
    loop_file.write_text(blocks(ROOT / 'docs/harness/loopflow-adapter-demo.md', 'loop')[0])
    failure = root / 'last-failure.txt'
    failure.write_text('controlled local Python test failure')
    arguments = [
        ['--goal', 'mcp agent loop local ollama filesystem', '--loop-name', 'ctx capability selection', '--permissions', 'skills,agents,mcps,harnesses', '--own-llm', '--model-provider', 'ollama', '--model', 'ollama/llama3.1', '--selected', 'local-ollama-file-operations', '--rejected', 'legacy-reviewer', '--top-k', '2'],
        ['--loop-file', str(loop_file), '--permissions', 'skills,agents,mcps,harnesses', '--own-llm', '--model-provider', 'ollama', '--model', 'ollama/llama3.1'],
        ['--loop-file', str(loop_file), '--permissions', 'skills,agents,mcps'],
        ['--loop-file', str(loop_file), '--permissions', 'skills,agents,mcps', '--selected', 'local-ollama-file-operations', '--rejected', 'legacy-reviewer'],
        ['--loop-file', str(loop_file), '--permissions', 'skills,agents,mcps,harnesses', '--own-llm', '--model-provider', 'ollama', '--model', 'ollama/llama3.1', '--harness-runtime', 'local workstation', '--harness-tools', 'filesystem, shell, browser', '--harness-privacy', 'no cloud prompts'],
        ['--loop-file', str(loop_file), '--permissions', 'skills,mcps', '--last-failure-file', str(failure)],
    ]
    before = {str(path.relative_to(wiki)): path.read_bytes() for path in wiki.rglob('*') if path.is_file()}
    for args in arguments:
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = loopflow.main(args)
        payload = json.loads(output.getvalue())
        assert code == 0 and payload['version'] == 'ctx.loop_adapter.v1'
        if '--own-llm' not in args:
            assert payload['capabilities']['harnesses'] == []
        if '--selected' in args:
            assert all(row['name'] != 'local-ollama-file-operations' for rows in payload['capabilities'].values() for row in rows)
        if '--last-failure-file' in args:
            assert payload['context']['last_failure_present'] is True
            assert failure.read_text() not in output.getvalue()
        results.append({'example': 'documented adapter CLI', 'argv': ['python', '-m', 'ctx.adapters.loopflow', *args], 'exit_code': code, 'payload': payload, 'stderr': error.getvalue()})
    assert before == {str(path.relative_to(wiki)): path.read_bytes() for path in wiki.rglob('*') if path.is_file()}

artifact = {'scope': 'DOC-NAV-008, DOC-NAV-027, B-DOC-001 and US-008 local recommendation examples',
    'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [attach, ROOT/'docs/harness/loopflow-adapter-demo.md', ROOT/'src/ctx/api.py', ROOT/'src/ctx/adapters/loopflow.py']},
    'isolation': 'synthetic real graph/wiki, actual public APIs and adapter CLI; only configured data roots and disabled telemetry redirected; no recommendation functions mocked',
    'results': results,
    'not_claimed': ['Claude SDK or external MCP-host execution', 'provider-backed ctx run/resume', 'external LoopFlow runner loading/unloading tools', 'fresh wheel install', 'live third-party server probes'],
    'primary_source_review': {'sdk_config': 'https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/types.py', 'finding': 'McpServerConfig is a union of TypedDict shapes; use a stdio dictionary rather than calling the union'}}
artifact_path = Path('/tmp/ctx-feature-audit-doc-host-example-evidence.json')
artifact_path.write_text(json.dumps(artifact, indent=2) + '\n')
print(json.dumps({'examples': len(results), 'artifact': str(artifact_path)}))
