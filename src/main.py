import argparse
import json
from pathlib import Path
from .models import validate_request, parse_time
from .storage import Storage
from .tools import Tools
from .llm_client import LiveClient
from .mock_policy import MockPolicy
from .agent import Agent
from .trace import Trace


def main():
    parser = argparse.ArgumentParser(description='Content task planning agent; simulated fees, local plans only')
    parser.add_argument('--mode', choices=['mock', 'live'], required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--db', default='campaign.sqlite')
    parser.add_argument('--trace', required=True)
    parser.add_argument('--output')
    parser.add_argument('--now', help='Explicit simulation clock anchor, ISO timestamp with offset')
    parser.add_argument('--inject-failure', choices=['search_materials', 'generate_script', 'save_plan'])
    parser.add_argument('--failure-mode', choices=['once', 'always'], default='once')
    parser.add_argument('--max-steps', type=int, default=12)
    parser.add_argument('--max-tool-calls', type=int, default=10)
    args = parser.parse_args()
    trace = Trace(args.mode, 'not_called', 'invalid-request')
    store = None
    try:
        request, missing = validate_request(json.loads(Path(args.input).read_text(encoding='utf-8-sig')))
        trace.value['case_id'] = request['request_id']
        if missing:
            result = {'request_id': request['request_id'], 'status': 'needs_information', 'missing_fields': missing,
                      'reason': 'Provide budget and deadline; no tools executed', 'task_plan': [], 'steps': [],
                      'cost_total': 0, 'plan_status': 'not_saved', 'plan_id': None, 'published': False}
        else:
            store = Storage(args.db)
            store.bind(request, args.mode)
            fixed = parse_time(args.now) if args.now else None
            tools = Tools(request, store, clock=(lambda: fixed) if fixed else None,
                          inject_failure=args.inject_failure, failure_mode=args.failure_mode)
            client = MockPolicy(tools) if args.mode == 'mock' else LiveClient()
            trace.value['model'] = client.model
            trace.value['clock_anchor'] = args.now or 'wall_clock_utc_plus_simulated_elapsed'
            result = Agent(tools, client, trace, args.max_steps, args.max_tool_calls).run()
    except Exception as exc:
        result = {'status': 'request_conflict' if 'REQUEST_CONFLICT' in str(exc) else 'incomplete',
                  'reason': str(exc), 'plan_status': 'not_saved', 'published': False}
    finally:
        if store:
            store.close()
    trace.save(args.trace, result)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered, encoding='utf-8')
    return 0 if result['status'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
