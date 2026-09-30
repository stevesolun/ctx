"""Bounded local documentation examples; never use host config or paid agents."""
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
import behavior_miner
import council_runner
import intent_interview
import toolbox
import toolbox_config
import toolbox_verdict

results = []

def run(example, module, args, expected=0, stdin=''):
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch('sys.stdin', io.StringIO(stdin)):
        try:
            code = module.main(args)
        except SystemExit as exc:
            code = exc.code
    item = {'example': example, 'argv': ['python', '-m', module.__name__, *args],
            'exit_code': code, 'stdout': stdout.getvalue(), 'stderr': stderr.getvalue(),
            'isolation': 'actual CLI parser/main with only user-data paths relocated to a disposable fixture; no provider/agent execution'}
    results.append(item)
    assert code == expected, item
    return item

with tempfile.TemporaryDirectory(prefix='ctx-doc-examples-') as temp, contextlib.ExitStack() as stack:
    task_root = Path(temp)
    repo = task_root / 'repo'
    repo.mkdir()
    state = task_root / 'state'
    state.mkdir()
    config = state / 'toolboxes.json'
    for module in (toolbox_config, toolbox, intent_interview):
        if hasattr(module, 'global_config_path'):
            stack.enter_context(patch.object(module, 'global_config_path', lambda: config))
    for module in (council_runner, toolbox_verdict):
        stack.enter_context(patch.object(module, 'RUNS_DIR', state / 'toolbox-runs'))
    for name, filename in [('INTENT_LOG', 'intent-log.jsonl'), ('SKILL_MANIFEST', 'skill-manifest.json'), ('USER_PROFILE', 'user-profile.json')]:
        stack.enter_context(patch.object(behavior_miner, name, state / filename))
    original_cwd = Path.cwd()
    os.chdir(repo)
    stack.callback(os.chdir, original_cwd)

    run('toolbox starter fixture setup', toolbox, ['init'])
    assert toolbox_config.load_global().active == ()
    for args in (['list'], ['show', 'ship-it'], ['activate', 'ship-it'], ['validate']):
        run('configuration editing tools', toolbox, args)
    run('configuration original export example', toolbox, ['export'], expected=2)
    results[-1]['semantic_result'] = 'documentation defect: export requires a toolbox name'
    exported = run('configuration corrected named export', toolbox, ['export', 'ship-it'])
    export_path = repo / 'my-toolboxes.yaml'
    export_path.write_text(exported['stdout'])
    run('configuration import', toolbox, ['import', str(export_path)])
    run('overview manual trigger', toolbox, ['run', '--event', 'pre-commit'])
    overview = (ROOT / 'docs/toolbox/index.md').read_text()
    declaration = overview.split('```yaml\n', 1)[1].split('```', 1)[0]
    repo_config = repo / '.toolbox.yaml'
    repo_config.write_text(declaration)
    run('overview exact declaration validates', toolbox, ['validate'])
    emitted = run('overview exact active declaration emits review plan', toolbox, ['run', '--event', 'pre-commit'])
    assert any(json.loads(line)['toolbox'] == 'review' for line in emitted['stdout'].splitlines())
    repo_config.write_text('version: 1\ntoolboxes:\n  ship-it:\n    description: replacement\n')
    replacement = toolbox_config.merged().toolboxes['ship-it']
    assert replacement.post == ()
    assert replacement.budget.max_tokens != 200000
    repo_config.unlink()

    existing = set((state / 'toolbox-runs').glob('*.json'))
    plan = run('council dry-run', council_runner, ['plan', '--toolbox', 'ship-it', '--dry-run'])
    assert set((state / 'toolbox-runs').glob('*.json')) == existing
    assert json.loads(plan['stdout'])['budget_tokens'] == 200000
    run('council persist plan', council_runner, ['plan', '--toolbox', 'ship-it'])
    run('council history', council_runner, ['history', '--limit', '10'])
    run('council purge disposable recent plans', council_runner, ['purge', '--older-than-days', '30'])

    for args in (['profile'], ['profile', '--save'], ['suggest', '--limit', '5'], ['suggest', '--save']):
        run('behavior miner CLI', behavior_miner, args)
    assert (state / 'user-profile.json').is_file()
    run('intent detect', intent_interview, ['detect'])
    for preset in ('blank', 'existing', 'docs-heavy', 'security-first'):
        result = run('intent preset ' + preset, intent_interview, ['init', '--preset', preset, '--apply'])
        assert json.loads(result['stdout'])['applied'] is True
    run('intent structured apply', intent_interview, ['init', '--non-interactive', '--starters', 'ship-it,security-sweep', '--suggestions', '1,2', '--analysis', 'dynamic', '--apply'])
    config_before = config.read_bytes()
    run('intent interactive skip', intent_interview, ['init'], stdin='skip\n')
    assert config.read_bytes() == config_before

    # This checks the original unquoted evidence example before correcting it.
    raw_args = shlex.split('record --plan-hash abc123 --level HIGH --title "SQL injection in users.py" --agent security-reviewer --evidence src/users.py:42:unescaped input --rationale "req.form values flow into raw SQL"')
    original = run('verdict original unquoted evidence example', toolbox_verdict, raw_args)
    original_evidence = json.loads(original['stdout'])['findings'][0]['evidence']
    assert len(original_evidence) == 2
    results[-1]['semantic_result'] = 'documentation defect: intended single evidence note split into two evidence records'
    corrected = run('verdict corrected quoted evidence and stable id', toolbox_verdict,
        ['record', '--plan-hash', 'fixed123', '--id', 'users-sql-injection', '--level', 'HIGH', '--title', 'SQL injection in users.py', '--agent', 'security-reviewer', '--evidence', 'src/users.py:42:unescaped input', '--rationale', 'req.form values flow into raw SQL'])
    assert json.loads(corrected['stdout'])['findings'][0]['evidence'] == [{'file': 'src/users.py', 'line': 42, 'note': 'unescaped input'}]
    for args in (['show', '--plan-hash', 'fixed123'], ['show', '--plan-hash', 'fixed123', '--json'], ['retro', '--limit', '10'], ['retro', '--min-level', 'HIGH'], ['explain', '--plan-hash', 'fixed123'], ['clear', '--plan-hash', 'fixed123', '--id', 'users-sql-injection']):
        run('verdict view/history/clear', toolbox_verdict, args)

artifact = {
    'scope': 'DOC-NAV-010 through DOC-NAV-017 and PKG-002; bounded safe example evidence, not a feature tracker',
    'source_hashes': {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in [*sorted((ROOT / 'docs/toolbox').glob('*.md')), *(ROOT / 'src' / name for name in ['toolbox.py', 'toolbox_config.py', 'toolbox_hooks.py', 'toolbox_verdict.py', 'council_runner.py', 'intent_interview.py', 'behavior_miner.py'])]},
    'additional_assertions': ['The exact minimal YAML declaration validates and emits the active review plan.', 'A same-name repository toolbox replaces the complete global toolbox; missing post/budget values use defaults.'],
    'results': results,
    'not_claimed': ['external host agent dispatch', 'paid model execution', 'installed Git hook behavior', 'a new verdict generated automatically during the hook call'],
}
Path('/tmp/ctx-feature-audit-doc-toolbox-example-evidence.json').write_text(json.dumps(artifact, indent=2) + '\n')
print(json.dumps({'examples_executed': len(results), 'semantic_defects_reproduced': 2, 'artifact': '/tmp/ctx-feature-audit-doc-toolbox-example-evidence.json'}))
