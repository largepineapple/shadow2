import json
from .llm_client import SYSTEM, TOOL_PARAMETERS


class Agent:
    def __init__(self, tools, client, trace, max_steps=12, max_tool_calls=10):
        self.tools, self.client, self.trace = tools, client, trace
        self.max_steps, self.max_calls = max_steps, max_tool_calls

    def run(self):
        if self.tools.storage.saved(self.tools.request_id):
            self.tools.save_status = 'existing'
            self.trace.add('cache_hit', {}, {'source': 'application', 'reason': 'Existing saved plan; no model call'})
            return self.result()
        messages = [{'role': 'system', 'content': SYSTEM},
                    {'role': 'user', 'content': json.dumps({'request': self.tools.request, 'persisted_state': self.tools.snapshot(), 'tool_costs': self.tools.costs})}]
        calls, errors = 0, 0
        for _ in range(self.max_steps):
            try:
                name, args, call_id = self.client.choose(messages)
                if name not in TOOL_PARAMETERS or not isinstance(args, dict) or set(args) != set(TOOL_PARAMETERS[name]):
                    raise ValueError('Invalid model action or arguments')
                if any(type(v) is not str for v in args.values()):
                    raise ValueError('Tool arguments must be strings')
            except Exception as exc:
                errors += 1
                self.trace.add('model_error', {}, {'error': str(exc)})
                if errors >= 2:
                    return self.result('incomplete', 'Model failure: ' + str(exc))
                messages.append({'role': 'user', 'content': 'Return exactly one valid tool call; previous response was invalid/unavailable.'})
                continue
            if name == 'finish':
                self.trace.add(name, args, {'ok': True}, args['decision_basis'])
                return self.result()
            if calls >= self.max_calls:
                return self.result('incomplete', 'max_tool_calls limit reached')
            observation = self.tools.execute(name, args)
            calls += 1
            self.trace.add(name, args, observation)
            messages.append({'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]})
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'name': name, 'content': json.dumps(observation)})
        return self.result('incomplete', 'max_steps limit reached')

    def result(self, forced=None, reason=None):
        state = self.tools.snapshot()
        saved = self.tools.storage.saved(self.tools.request_id)
        strategy = state['strategy']
        quote = self.tools._quote(strategy) if strategy else None
        failed = [s for s in state['steps'] if s['status'] == 'failed']
        if saved:
            status, reason_default = 'completed', 'Local content plan saved; nothing published'
        elif quote and quote['budget_gap']:
            status, reason_default = 'budget_exceeded', 'No selected plan fits the budget; inspect budget_gap'
        elif quote and quote['deadline_gap_seconds']:
            status, reason_default = 'deadline_exceeded', 'No selected plan fits the deadline; inspect deadline_gap_seconds'
        elif any(s['step_key'] == 'save' for s in failed):
            status, reason_default = 'save_failed', 'Save tool failed; successful previous steps retained'
        else:
            status, reason_default = 'incomplete', 'Failed or pending steps remain; successful results retained'
        blockers = []
        if not saved:
            if quote and quote['budget_gap']:
                blockers.append({'code': 'BUDGET_EXCEEDED', 'budget_gap': quote['budget_gap']})
            if quote and quote['deadline_gap_seconds']:
                blockers.append({'code': 'DEADLINE_EXCEEDED', 'deadline_gap_seconds': quote['deadline_gap_seconds']})
            active_steps = {s['step_id'] for s in state['task_plan']}
            blockers.extend({'code': 'TOOL_FAILURE', 'step_id': s['step_key'], 'tool': s['tool'], 'error': s['error']}
                            for s in failed if s['step_key'] in active_steps)
            if not blockers:
                blockers.append({'code': 'PENDING_STEPS', 'step_ids': [s['step_id'] for s in state['task_plan'] if s['status'] != 'succeeded']})
        if forced:
            blockers.append({'code': 'EXECUTION_INCOMPLETE', 'message': reason})
        return {**state, 'status': forced or status, 'reason': reason or reason_default,
                'blocking_reasons': blockers,
                'estimate': quote, 'plan_status': self.tools.save_status if saved else 'not_saved',
                'plan_id': saved['plan_id'] if saved else None,
                'saved_plan': saved['content'] if saved else None, 'published': False}
