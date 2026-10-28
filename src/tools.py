import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .models import canonical, parse_time


def ok(data):
    return {'ok': True, 'data': data, 'error': None}


def fail(code, message, **details):
    return {'ok': False, 'data': None, 'error': {'code': code, 'message': message, **details}}


class Tools:
    def __init__(self, request, storage, clock=None, inject_failure=None, failure_mode='once'):
        self.request, self.storage = request, storage
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.inject_failure, self.failure_mode = inject_failure, failure_mode
        self.save_status = 'not_saved'
        root = Path(__file__).resolve().parents[1] / 'data'
        self.costs = json.loads((root / 'tool_costs.json').read_text(encoding='utf-8'))
        self.materials = json.loads((root / 'materials.json').read_text(encoding='utf-8'))

    @property
    def request_id(self):
        return self.request['request_id']

    def _steps(self):
        return {s['step_key']: s for s in self.storage.steps(self.request_id)}

    def active_strategy(self):
        row = self.storage.db.execute('SELECT active_strategy FROM jobs WHERE request_id=?', (self.request_id,)).fetchone()
        return row['active_strategy']

    def _quote(self, strategy):
        steps = self._steps()
        spent = sum(s['cost'] for s in steps.values())
        elapsed = sum(s['seconds'] for s in steps.values())
        remaining_cost, remaining_seconds = 0, 0
        for key, fee in [('search', self.costs['search_materials']),
                         ('script:' + strategy, self.costs['generate_script'][strategy])]:
            if steps.get(key, {}).get('status') != 'succeeded':
                remaining_cost += fee['cost']
                remaining_seconds += fee['seconds']
        total = spent + remaining_cost
        projected = self.clock() + timedelta(seconds=elapsed + remaining_seconds)
        budget_gap = max(0, total - self.request['budget'])
        deadline_gap = max(0, int((projected - parse_time(self.request['deadline'])).total_seconds() + 0.999999))
        return {'strategy': strategy, 'estimated_total': total, 'incurred_cost': spent,
                'remaining_cost': remaining_cost, 'simulated_elapsed_seconds': elapsed,
                'remaining_seconds': remaining_seconds, 'projected_finish': projected.isoformat(),
                'budget_gap': budget_gap, 'deadline_gap_seconds': deadline_gap,
                'feasible': budget_gap == 0 and deadline_gap == 0,
                'available_strategies': self.costs['generate_script']}

    def _record(self, key, tool, observation, cost=0, seconds=0):
        self.storage.db.execute('''INSERT INTO steps(request_id,step_key,tool,status,result_json,error_json,cost,seconds,attempts)
            VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(request_id,step_key) DO UPDATE SET
            status=excluded.status,result_json=excluded.result_json,error_json=excluded.error_json,
            cost=excluded.cost,seconds=excluded.seconds,attempts=steps.attempts+1''',
            (self.request_id, key, tool, 'succeeded' if observation['ok'] else 'failed',
             canonical(observation['data']), canonical(observation['error']), cost, seconds))

    def snapshot(self):
        steps = self.storage.steps(self.request_id)
        strategy = self.active_strategy()
        statuses = {s['step_key']: s['status'] for s in steps}
        keys = ['estimate:' + strategy, 'search', 'script:' + strategy, 'save'] if strategy else ['estimate', 'search', 'script', 'save']
        dependencies = [[], [keys[0]], [keys[0], 'search'], [keys[2]]]
        plan = [{'step_id': key, 'depends_on': dep, 'status': statuses.get(key, 'pending')} for key, dep in zip(keys, dependencies)]
        return {'request_id': self.request_id, 'theme': self.request['theme'], 'budget': self.request['budget'],
                'deadline': self.request['deadline'], 'strategy': strategy, 'task_plan': plan,
                'steps': steps, 'cost_total': sum(s['cost'] for s in steps),
                'simulated_elapsed_seconds': sum(s['seconds'] for s in steps)}

    def _fault(self, tool):
        if self.inject_failure != tool:
            return False
        db = self.storage.db
        found = db.execute('SELECT 1 FROM injected_failures WHERE request_id=? AND tool=?', (self.request_id, tool)).fetchone()
        db.execute('INSERT OR IGNORE INTO injected_failures VALUES (?,?)', (self.request_id, tool))
        return self.failure_mode == 'always' or not found

    def execute(self, name, arguments):
        schemas = {'estimate_cost': {'strategy'}, 'search_materials': set(),
                   'generate_script': {'strategy'}, 'save_plan': set()}
        if name not in schemas or not isinstance(arguments, dict) or set(arguments) != schemas[name]:
            return fail('INVALID_ARGUMENT', 'Unknown tool or unexpected arguments')
        strategy = arguments.get('strategy')
        if strategy is not None and (type(strategy) is not str or strategy not in self.costs['generate_script']):
            return fail('INVALID_STRATEGY', 'Choose premium or basic')
        try:
            with self.storage.transaction():
                return self._execute(name, strategy)
        except Exception as exc:
            return fail('TOOL_ERROR', str(exc))

    def _execute(self, name, strategy):
        db = self.storage.db
        if name == 'estimate_cost':
            quote = self._quote(strategy)
            db.execute('UPDATE jobs SET active_strategy=? WHERE request_id=?', (strategy, self.request_id))
            observation = ok(quote)
            self._record('estimate:' + strategy, name, observation)
            observation['data']['task_plan'] = self.snapshot()['task_plan']
            return observation
        key = {'search_materials': 'search', 'generate_script': 'script:' + str(strategy), 'save_plan': 'save'}[name]
        existing = self._steps().get(key)
        if existing and existing['status'] == 'succeeded':
            if name == 'save_plan': self.save_status = 'existing'
            return ok({'result': existing['result'], 'reused': True, 'charged_now': 0})
        active = self.active_strategy()
        if not active or (name == 'generate_script' and active != strategy):
            return fail('DEPENDENCY_NOT_MET', 'Estimate this strategy before executing it')
        quote = self._quote(active)
        if not quote['feasible']:
            code = 'BUDGET_EXCEEDED' if quote['budget_gap'] else 'DEADLINE_EXCEEDED'
            return fail(code, 'Active plan violates input constraints', quote=quote)
        steps = self._steps()
        if name == 'generate_script' and steps.get('search', {}).get('status') != 'succeeded':
            return fail('DEPENDENCY_NOT_MET', 'Material search must succeed before script generation')
        if name == 'save_plan' and steps.get('script:' + active, {}).get('status') != 'succeeded':
            return fail('DEPENDENCY_NOT_MET', 'Selected script must succeed before saving')
        if self._fault(name):
            observation = fail('INJECTED_TOOL_FAILURE', 'Explicit failure before execution; no simulated fee charged')
            self._record(key, name, observation)
            return observation
        if name == 'search_materials':
            words = set(self.request['theme'].lower().split())
            matches = [m for m in self.materials if words.intersection(m['tags'])] or self.materials[:1]
            result = {'materials': matches, 'source': 'synthetic_local_catalog'}
            fee = self.costs[name]
        elif name == 'generate_script':
            materials = steps['search']['result']['materials']
            result = {'strategy': strategy, 'generator': 'synthetic_template',
                      'material_ids': [m['id'] for m in materials],
                      'script': {'hook': 'Discover ' + self.request['theme'],
                                 'scenes': [m['description'] for m in materials],
                                 'call_to_action': 'Join our ' + self.request['theme'] + ' activity'},
                      'deliverables': ['short video', 'caption', 'storyboard'] if strategy == 'premium' else ['short video', 'caption']}
            fee = self.costs[name][strategy]
        else:
            cur = db.execute('INSERT INTO plans(request_id,content_json) VALUES (?,?)', (self.request_id, '{}'))
            result, fee = {'plan_id': cur.lastrowid}, self.costs[name]
            self.save_status = 'saved'
        observation = ok(result)
        self._record(key, name, observation, fee['cost'], fee['seconds'])
        if name == 'save_plan':
            db.execute('UPDATE plans SET content_json=? WHERE request_id=?', (canonical(self.snapshot()), self.request_id))
        return ok({'result': result, 'reused': False, 'charged_now': fee['cost']})
