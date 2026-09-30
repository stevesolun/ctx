"""Execute documented local health/quality/lifecycle commands on disposable data."""
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from ctx.adapters.claude_code import skill_health
import ctx_audit_log
import ctx_config
import ctx_lifecycle
import kpi_dashboard
import memory_anchor
import skill_category
import skill_quality

results = []
def run(example, module, args, expected=0, stdin=''):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch('sys.stdin', io.StringIO(stdin)):
        try:
            code = module.main(args)
        except SystemExit as exc:
            code = exc.code
    item = {'example': example, 'argv': ['python', '-m', module.__name__, *args], 'exit_code': code,
            'stdout': stdout.getvalue(), 'stderr': stderr.getvalue(),
            'isolation': 'real CLI parser/main; only module data/config paths redirected to disposable fixture; no host services or paid calls'}
    results.append(item)
    assert code == expected, item
    return item

with tempfile.TemporaryDirectory(prefix='ctx-doc-lifecycle-') as temp, contextlib.ExitStack() as stack:
    root = Path(temp)
    skills, agents, wiki, sidecars, memory = [root / name for name in ('skills', 'agents', 'wiki', 'quality', 'memory')]
    for path in (skills, agents, wiki, sidecars, memory):
        path.mkdir()
    slug = 'python-testing'
    installed = skills / slug / 'SKILL.md'
    installed.parent.mkdir()
    installed.write_text('---\nname: python-testing\ndescription: Test Python code.\ntags: [python, testing]\n---\n# Testing\n\nPlan cases.\nArrange inputs.\nRun checks.\nInspect outputs.\nExplain failures.\n')
    original = installed.read_bytes()
    manifest, pending = root / 'manifest.json', root / 'pending.json'
    manifest.write_text(json.dumps({'load': [{'skill': slug}, {'skill': 'orphan'}]}))
    pending.write_text(json.dumps({'graph_suggestions': [{'name': slug}, {'name': 'orphan'}], 'unmatched_signals': []}))
    for name, path in [('SKILLS_DIR', skills), ('AGENTS_DIR', agents), ('MANIFEST_PATH', manifest), ('PENDING_PATH', pending)]:
        stack.enter_context(patch.object(skill_health, name, path))
    stack.enter_context(patch.object(ctx_audit_log, 'audit_log_path', lambda: root / 'audit.jsonl'))
    stack.enter_context(patch.object(ctx_config, 'cfg', SimpleNamespace(skills_dir=skills, agents_dir=agents)))
    for args in (['scan'], ['dashboard']):
        run('health report with real orphan fixture', skill_health, args)
    run('health strict refuses drift', skill_health, ['check', '--strict'], expected=2)
    run('health heal orphan-only mutation', skill_health, ['heal'])
    assert installed.read_bytes() == original
    assert json.loads(manifest.read_text())['load'] == [{'skill': slug}]
    assert json.loads(pending.read_text())['graph_suggestions'] == [{'name': slug}]
    run('health strict passes healed data', skill_health, ['check', '--strict'])
    run('health heal idempotent no-op', skill_health, ['heal'])

    notes = memory / 'notes.md'
    notes.write_text('Live `skills/python-testing/SKILL.md:1`; dead `src/missing.py`.\n')
    stack.enter_context(patch.object(memory_anchor, 'DEFAULT_MEMORY_ROOT', memory))
    for args in (['scan'], ['dashboard']):
        run('memory references scan', memory_anchor, [*args, '--repo-root', str(root)])
    run('memory strict reports dead reference', memory_anchor, ['check', '--strict', '--repo-root', str(root), '--memory-root', str(memory)], expected=2)
    notes.write_text('Live `skills/python-testing/SKILL.md:1`.\n')
    run('memory strict passes live reference', memory_anchor, ['check', '--strict', '--repo-root', str(root), '--memory-root', str(memory)])

    run('category backfill dry-run', skill_category, ['backfill', '--dry-run'])
    assert installed.read_bytes() == original
    run('category backfill apply', skill_category, ['backfill'])
    assert 'category:' in installed.read_text()
    sources = skill_quality.SignalSources(skills, agents, wiki, root / 'events.jsonl')
    stack.enter_context(patch.object(skill_quality, '_build_sources_from_config', lambda: sources))
    stack.enter_context(patch.object(skill_quality, '_config_from_cfg', skill_quality.QualityConfig))
    stack.enter_context(patch.object(skill_quality, 'default_sidecar_dir', lambda: sidecars))
    for args in (['recompute', '--all'], ['recompute', '--slug', slug], ['show', slug], ['explain', slug], ['list'], ['list', '--grade', 'D'], ['show', slug, '--json']):
        run('quality documented CLI', skill_quality, args)
    assert (sidecars / f'{slug}.json').is_file()
    score = skill_quality.load_quality(slug, sidecar_dir=sidecars)
    assert score is not None
    assert score.grade in {'A', 'B', 'C', 'D', 'F'}

    lifecycle_sources = ctx_lifecycle.LifecycleSources(skills, agents, sidecars)
    stack.enter_context(patch.object(ctx_lifecycle, '_build_sources', lambda: lifecycle_sources))
    stack.enter_context(patch.object(ctx_lifecycle, '_build_config', ctx_lifecycle.LifecycleConfig))
    lifecycle_path = ctx_lifecycle.lifecycle_sidecar_path(slug, sidecar_dir=sidecars)
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    dry_run = run('lifecycle populated dry-run write audit', ctx_lifecycle, ['review', '--dry-run'])
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*') if path.is_file()}
    dry_run['changed_paths'] = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    dry_run['semantic_result'] = 'defect: dry-run writes lifecycle state' if before != after else 'no fixture file changed'
    run('lifecycle safe auto review', ctx_lifecycle, ['review', '--auto'])
    run('lifecycle explicit demote declined', ctx_lifecycle, ['demote', slug], expected=1, stdin='n\n')
    assert installed.is_file()
    run('lifecycle explicit demote confirmed', ctx_lifecycle, ['demote', slug], stdin='y\n')
    demoted = skills / '_demoted' / slug / 'SKILL.md'
    assert demoted.is_file() and not installed.exists()
    run('lifecycle explicit archive declined', ctx_lifecycle, ['archive', slug], expected=1, stdin='n\n')
    assert demoted.is_file()
    run('lifecycle explicit archive confirmed without age floor', ctx_lifecycle, ['archive', slug], stdin='y\n')
    archived = skills / '_archive' / slug / 'SKILL.md'
    assert archived.is_file() and not demoted.exists()
    run('lifecycle original invalid restore slug', ctx_lifecycle, ['review-archived', '--restore', slug], expected=2)
    results[-1]['semantic_result'] = 'documentation defect: --restore is a boolean flag, not a slug argument'
    run('lifecycle archived list and diff', ctx_lifecycle, ['review-archived', '--show-diff'])
    run('lifecycle corrected restore declined', ctx_lifecycle, ['review-archived', '--restore'], stdin='n\n')
    assert archived.is_file()
    run('lifecycle corrected restore confirmed', ctx_lifecycle, ['review-archived', '--restore'], stdin='y\n')
    assert installed.is_file() and not archived.exists()
    run('lifecycle archive setup demote forced', ctx_lifecycle, ['demote', slug, '--force'])
    run('lifecycle archive setup archive forced', ctx_lifecycle, ['archive', slug, '--force'])
    state = ctx_lifecycle.load_lifecycle_state(slug, sidecar_dir=sidecars)
    ctx_lifecycle.save_lifecycle_state(replace(state, state_since=(datetime.now(timezone.utc) - timedelta(days=90)).isoformat()), sidecar_dir=sidecars)
    run('lifecycle purge wrong slug preserves fixture', ctx_lifecycle, ['purge'], stdin='wrong-slug\n')
    assert archived.is_file()
    run('lifecycle purge exact slug deletes only disposable fixture', ctx_lifecycle, ['purge'], stdin=slug + '\n')
    assert not archived.exists()

    stack.enter_context(patch.object(kpi_dashboard, '_build_sources_from_config', lambda: lifecycle_sources))
    for args in (['render'], ['render', '--out', str(root / 'kpi.md')], ['render', '--json', '--out', str(root / 'kpi.json')], ['summary']):
        run('KPI documented CLI', kpi_dashboard, args)
    assert (root / 'kpi.md').is_file()
    assert isinstance(json.loads((root / 'kpi.json').read_text()), dict)

docs = ['skills-health.md', 'memory-anchor.md', 'skill-lifecycle-and-dashboard.md', 'skill-quality-install.md']
sources = ['ctx/adapters/claude_code/skill_health.py', 'memory_anchor.py', 'ctx_lifecycle.py', 'skill_quality.py', 'skill_category.py', 'kpi_dashboard.py']
artifact = {'scope': 'DOC-NAV-023 through DOC-NAV-026; local example evidence only, not canonical tracker',
    'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [*(ROOT/'docs'/name for name in docs), *(ROOT/'src'/name for name in sources)]},
    'results': results,
    'not_claimed': ['host Stop hook installation/execution', 'full installed-corpus timings', 'external host lifecycle', 'paid model execution']}
path = Path('/tmp/ctx-feature-audit-doc-lifecycle-example-evidence.json')
path.write_text(json.dumps(artifact, indent=2) + '\n')
print(json.dumps({'examples': len(results), 'artifact': str(path), 'dry_run': dry_run['semantic_result'], 'dry_run_changed_paths': dry_run['changed_paths']}))
