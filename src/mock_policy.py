class MockPolicy:
    model = 'deterministic-mock-no-LLM'

    def __init__(self, tools):
        self.tools = tools
        self.baseline = {s['step_key']: s['attempts'] for s in tools.snapshot()['steps']}

    def choose(self, messages):
        state = self.tools.snapshot()
        steps = {s['step_key']: s for s in state['steps']}
        strategy = state['strategy']
        if not strategy:
            return 'estimate_cost', {'strategy': 'premium'}, 'mock'
        quote = self.tools._quote(strategy)
        if not quote['feasible']:
            if strategy == 'premium':
                return 'estimate_cost', {'strategy': 'basic'}, 'mock'
            return 'finish', {'decision_basis': 'No feasible synthetic alternative'}, 'mock'
        for key, tool, args in [('search', 'search_materials', {}),
                                ('script:' + strategy, 'generate_script', {'strategy': strategy}),
                                ('save', 'save_plan', {})]:
            step = steps.get(key)
            if step and step['status'] == 'succeeded':
                continue
            if step and step['status'] == 'failed' and step['attempts'] > self.baseline.get(key, 0):
                return 'finish', {'decision_basis': 'Mock stops on this-run tool error; resume retains successes'}, 'mock'
            return tool, args, 'mock'
        return 'finish', {'decision_basis': 'Observed local plan save'}, 'mock'
