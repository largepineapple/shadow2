"""Run isolated CLI acceptance cases; live genuinely calls the configured model."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing

CASES = ['normal', 'replan', 'missing_budget', 'missing_deadline', 'tool_failure',
         'over_budget', 'deadline', 'duplicate', 'save_failure', 'resume_failed', 'resume_completed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['mock', 'live'], required=True)
    parser.add_argument('--cases', nargs='+', choices=CASES, default=CASES)
    parser.add_argument('--prefix')
    args = parser.parse_args()
    prefix = args.prefix or args.mode
    if not prefix.replace('_', '').replace('-', '').isalnum():
        parser.error('prefix must be a simple filename component')
    selected = list(args.cases)
    if 'duplicate' in selected and 'normal' not in selected:
        selected.insert(0, 'normal')
    if 'resume_completed' in selected and 'resume_failed' not in selected:
        selected.insert(0, 'resume_failed')
    summary = {'mode': args.mode, 'clock_anchor': '2026-10-28T12:00:00+00:00', 'cases': []}
    started = time.perf_counter()
    outputs = {}
    with tempfile.TemporaryDirectory() as directory:
        for name in selected:
            source = 'normal' if name == 'duplicate' else 'resume' if name.startswith('resume_') else name
            db = Path(directory) / ('normal' if name == 'duplicate' else 'resume_failed' if name == 'resume_completed' else name)
            stem = prefix + '_' + name
            command = [sys.executable, '-m', 'src.main', '--mode', args.mode,
                       '--input', f'examples/inputs/{source}.json', '--db', str(db),
                       '--now', summary['clock_anchor'], '--trace', f'examples/traces/{stem}.json',
                       '--output', f'examples/outputs/{stem}.json']
            if name in ('tool_failure', 'resume_failed'):
                command += ['--inject-failure', 'generate_script', '--failure-mode', 'always']
            if name == 'save_failure':
                command += ['--inject-failure', 'save_plan', '--failure-mode', 'always']
            before = time.perf_counter()
            run = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=600)
            if not run.stdout:
                raise RuntimeError(run.stderr)
            result = json.loads(run.stdout)
            outputs[name] = result
            trace = json.loads(Path(f'examples/traces/{stem}.json').read_text(encoding='utf-8'))
            expected = {'normal':'completed', 'replan':'completed', 'missing_budget':'needs_information',
                        'missing_deadline':'needs_information', 'tool_failure':'incomplete',
                        'over_budget':'budget_exceeded', 'deadline':'deadline_exceeded',
                        'duplicate':'completed', 'save_failure':'save_failed',
                        'resume_failed':'incomplete', 'resume_completed':'completed'}[name]
            with closing(sqlite3.connect(db)) as connection:
                exists = connection.execute("SELECT name FROM sqlite_master WHERE name='plans'").fetchone()
                count = connection.execute('SELECT COUNT(*) FROM plans').fetchone()[0] if exists else 0
            checks = {'status': result['status'] == expected, 'not_published': result['published'] is False,
                      'db_count': count == (1 if expected == 'completed' else 0),
                      'exit_code': run.returncode == (0 if expected == 'completed' else 1)}
            if 'cost_total' in result:
                checks['ledger'] = result['cost_total'] == sum(s['cost'] for s in result.get('steps', []))
            if name in ('normal','replan','tool_failure','resume_failed','save_failure'):
                checks['expected_cost'] = result.get('cost_total') == {'normal':12,'replan':8,'tool_failure':2,'resume_failed':2,'save_failure':12}[name]
            if name == 'replan':
                quotes = [s['observation_summary']['data'] for s in trace['steps'] if s['action']=='estimate_cost' and s['observation_summary']['ok']]
                checks['replanned_12_to_8'] = any(q['estimated_total']==12 and not q['feasible'] for q in quotes) and any(q['estimated_total']==8 and q['feasible'] for q in quotes)
            if name == 'duplicate':
                checks['same_plan'] = result.get('plan_id') == outputs['normal'].get('plan_id')
                checks['no_recharge'] = result.get('cost_total') == outputs['normal'].get('cost_total')
                checks['existing'] = result.get('plan_status') == 'existing'
            if name == 'resume_completed':
                search = next((s for s in result.get('steps',[]) if s['step_key']=='search'), {})
                checks['search_reused'] = search.get('attempts') == 1 and search.get('status') == 'succeeded'
                checks['paid_once'] = result.get('cost_total') in (8,12)
            if name.startswith('missing_'):
                checks['no_tools_or_model'] = not trace['steps'] and trace['model']=='not_called' and result['cost_total']==0
            elif name != 'duplicate':
                checks['loop_observed'] = any(s['action'] not in ('model_error','finish') and s['next_action'] is not None for s in trace['steps'])
            entry = {'name': name, 'input': f'examples/inputs/{source}.json', 'status': result['status'],
                     'cost_total': result.get('cost_total'), 'plan_id': result.get('plan_id'), 'db_count': count,
                     'model': trace['model'], 'actions': [s['action'] for s in trace['steps']],
                     'checks': checks, 'passed': all(checks.values()),
                     'elapsed_seconds': round(time.perf_counter()-before,3)}
            summary['cases'].append(entry)
            summary['elapsed_seconds'] = round(time.perf_counter()-started,3)
            Path(f'examples/{prefix}_results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(entry,ensure_ascii=False),flush=True)
            if any(s['action']=='model_error' for s in trace['steps']) and not any(s['action'] not in ('model_error','finish') for s in trace['steps']):
                print('Stopping after model connection/response failure',flush=True)
                break
    return 0 if len(summary['cases'])==len(selected) and all(c['passed'] for c in summary['cases']) else 1


if __name__=='__main__':
    raise SystemExit(main())
