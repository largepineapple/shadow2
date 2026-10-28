import json
from datetime import timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from src.models import validate_request, parse_time
from src.storage import Storage
from src.tools import Tools
from src.agent import Agent
from src.mock_policy import MockPolicy
from src.trace import Trace

NOW = parse_time('2026-10-28T12:00:00+00:00')


def request(name='normal'):
    return json.loads(Path(f'examples/inputs/{name}.json').read_text(encoding='utf-8'))


def setup(tmp_path, name='normal', failure=None, failure_mode='once', clock=None):
    r, missing = validate_request(request(name))
    assert not missing
    store = Storage(str(tmp_path / 'test.sqlite'))
    store.bind(r, 'mock')
    return Tools(r, store, clock=clock, simulation_start=None if clock else NOW,
                 inject_failure=failure, failure_mode=failure_mode)


def run(tools, **limits):
    trace = Trace('mock', 'deterministic-mock-no-LLM', tools.request_id)
    result = Agent(tools, MockPolicy(tools), trace, **limits).run()
    return result, trace


@pytest.mark.parametrize('name,status,cost', [
    ('normal','completed',12), ('replan','completed',8),
    ('over_budget','budget_exceeded',0), ('deadline','deadline_exceeded',0),
])
def test_scenarios(tmp_path,name,status,cost):
    tools=setup(tmp_path,name)
    result,trace=run(tools)
    assert result['status']==status
    assert result['cost_total']==cost
    assert result['published'] is False
    assert bool(result['blocking_reasons']) == (status!='completed')
    assert bool(result['saved_plan']) == (status=='completed')
    if name=='replan':
        quotes=[s['observation_summary']['data'] for s in trace.value['steps'] if s['action']=='estimate_cost']
        assert [q['estimated_total'] for q in quotes]==[12,8]
        assert [q['feasible'] for q in quotes]==[False,True]
        assert result['strategy']=='basic'
    if name=='over_budget': assert result['estimate']['budget_gap']==1
    if name=='deadline': assert result['estimate']['deadline_gap_seconds']==180
    if status=='completed': assert all(s['status']=='succeeded' for s in result['task_plan'])
    tools.storage.close()


@pytest.mark.parametrize('field',['budget','deadline'])
def test_missing_information(field):
    r=request();r.pop(field)
    _,missing=validate_request(r)
    assert missing==[field]


@pytest.mark.parametrize('budget',[-1,True,1.5,'10'])
def test_invalid_budget(budget):
    r=request();r['budget']=budget
    with pytest.raises(ValueError): validate_request(r)


def test_deadline_requires_offset():
    r=request();r['deadline']='2026-11-03T18:00:00'
    with pytest.raises(ValueError): validate_request(r)


def test_server_dependencies(tmp_path):
    tools=setup(tmp_path)
    assert tools.execute('generate_script',{'strategy':'basic'})['error']['code']=='DEPENDENCY_NOT_MET'
    assert tools.execute('save_plan',{})['error']['code']=='DEPENDENCY_NOT_MET'
    tools.execute('estimate_cost',{'strategy':'basic'})
    assert tools.execute('generate_script',{'strategy':'basic'})['error']['code']=='DEPENDENCY_NOT_MET'
    assert tools.snapshot()['cost_total']==0
    tools.storage.close()


def test_budget_cannot_be_overridden(tmp_path):
    tools=setup(tmp_path,'replan')
    tools.execute('estimate_cost',{'strategy':'premium'})
    assert tools.execute('search_materials',{})['error']['code']=='BUDGET_EXCEEDED'
    assert tools.execute('save_plan',{'budget':100})['error']['code']=='INVALID_ARGUMENT'
    assert tools.snapshot()['cost_total']==0
    assert not tools.storage.saved(tools.request_id)
    tools.storage.close()


def test_failure_preserves_success_and_resume(tmp_path):
    tools=setup(tmp_path,failure='generate_script')
    first,_=run(tools)
    assert first['status']=='incomplete'
    assert first['cost_total']==2
    assert tools._steps()['search']['status']=='succeeded'
    assert tools._steps()['script:premium']['status']=='failed'
    assert not tools.storage.saved(tools.request_id)
    second,_=run(tools)
    assert second['status']=='completed'
    assert second['cost_total']==12
    assert tools._steps()['search']['attempts']==1
    assert tools._steps()['script:premium']['attempts']==2
    tools.storage.close()


@pytest.mark.parametrize('failure,cost,status',[
    ('search_materials',0,'incomplete'),('generate_script',2,'incomplete'),('save_plan',12,'save_failed'),
])
def test_permanent_errors(tmp_path,failure,cost,status):
    tools=setup(tmp_path,failure=failure,failure_mode='always')
    result,_=run(tools)
    assert result['status']==status
    assert result['cost_total']==cost
    assert not result['saved_plan']
    assert any(s['status']=='failed' for s in result['steps'])
    tools.storage.close()


def test_duplicate_no_extra_charge(tmp_path):
    tools=setup(tmp_path)
    first,_=run(tools)
    second,trace=run(tools)
    assert first['plan_id']==second['plan_id']
    assert second['plan_status']=='existing'
    assert first['cost_total']==second['cost_total']==12
    assert len(list(tools.storage.db.execute('SELECT * FROM plans')))==1
    assert [s['action'] for s in trace.value['steps']]==['cache_hit']
    tools.storage.close()


@pytest.mark.parametrize('steps,calls',[(1,10),(12,0),(3,10)])
def test_limits_preserve_work(tmp_path,steps,calls):
    tools=setup(tmp_path)
    result,_=run(tools,max_steps=steps,max_tool_calls=calls)
    assert result['status']=='incomplete' and 'limit' in result['reason']
    assert any(r['code']=='EXECUTION_INCOMPLETE' for r in result['blocking_reasons'])
    if steps==3:
        assert result['cost_total']==12
        assert tools._steps()['script:premium']['status']=='succeeded'
        resumed,_=run(tools)
        assert resumed['status']=='completed' and resumed['cost_total']==12
    tools.storage.close()


def test_request_identity_and_mode(tmp_path):
    tools=setup(tmp_path)
    changed=tools.request|{'budget':1}
    with pytest.raises(ValueError,match='REQUEST_CONFLICT'): tools.storage.bind(changed,'mock')
    with pytest.raises(ValueError,match='REQUEST_CONFLICT'): tools.storage.bind(tools.request,'live')
    tools.storage.close()


def test_concurrent_idempotent_paid_step(tmp_path):
    tools=setup(tmp_path)
    tools.execute('estimate_cost',{'strategy':'premium'})
    tools.storage.close()
    def search(_):
        store=Storage(str(tmp_path/'test.sqlite'))
        r,_=validate_request(request());store.bind(r,'mock')
        t=Tools(r,store,clock=lambda:NOW)
        result=t.execute('search_materials',{})
        store.close()
        return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(search,range(2)))
    assert sorted(r['data']['charged_now'] for r in results)==[0,2]


def test_untrusted_finish_cannot_claim_success(tmp_path):
    class Bad:
        def choose(self,messages): return 'finish',{'decision_basis':'Everything saved successfully'},'bad'
    tools=setup(tmp_path)
    result=Agent(tools,Bad(),Trace('mock','test','bad')).run()
    assert result['status']=='incomplete'
    assert result['plan_id'] is None and result['cost_total']==0
    tools.storage.close()


def test_deadline_rechecked_after_partial_success(tmp_path):
    current=[NOW]
    tools=setup(tmp_path,clock=lambda:current[0])
    tools.request['deadline']='2026-10-28T12:05:00+00:00'
    tools.execute('estimate_cost',{'strategy':'basic'})
    assert tools.execute('search_materials',{})['ok']
    current[0]=NOW.replace(minute=5)
    result=tools.execute('generate_script',{'strategy':'basic'})
    assert result['error']['code']=='DEADLINE_EXCEEDED'
    assert tools.snapshot()['cost_total']==2
    assert tools._steps()['search']['status']=='succeeded'
    assert 'script:basic' not in tools._steps()
    tools.storage.close()


def test_invalid_tool_arguments_are_structured(tmp_path):
    tools=setup(tmp_path)
    assert tools.execute('estimate_cost',{'strategy':[]})['error']['code']=='INVALID_STRATEGY'
    assert tools.execute('shell',{'code':'anything'})['error']['code']=='INVALID_ARGUMENT'
    tools.storage.close()


@pytest.mark.parametrize('advance',[60,120])
def test_advancing_wall_clock_does_not_double_count_completed_work(tmp_path,advance):
    current=[NOW]
    tools=setup(tmp_path,clock=lambda:current[0])
    initial=tools.execute('estimate_cost',{'strategy':'basic'})['data']
    assert parse_time(initial['projected_finish'])==NOW+timedelta(seconds=240)
    assert tools.execute('search_materials',{})['ok']
    current[0]=NOW+timedelta(seconds=advance)
    quote=tools._quote('basic')
    assert quote['clock_mode']=='wall_clock'
    assert quote['simulated_elapsed_seconds']==60
    assert quote['remaining_seconds']==180
    assert parse_time(quote['projected_finish'])==current[0]+timedelta(seconds=180)
    tools.storage.close()


def test_simulation_clock_uses_anchor_and_ledger_on_resume(tmp_path):
    tools=setup(tmp_path)
    tools.execute('estimate_cost',{'strategy':'basic'})
    tools.execute('search_materials',{})
    resumed=Tools(tools.request,tools.storage,simulation_start=NOW,clock=lambda:NOW+timedelta(hours=1))
    quote=resumed._quote('basic')
    assert quote['clock_mode']=='simulation'
    assert parse_time(quote['current_time'])==NOW+timedelta(seconds=60)
    assert parse_time(quote['projected_finish'])==NOW+timedelta(seconds=240)
    tools.storage.close()


def test_wall_clock_exact_deadline_allows_remaining_work(tmp_path):
    current=[NOW]
    tools=setup(tmp_path,clock=lambda:current[0])
    tools.request['deadline']=(NOW+timedelta(seconds=240)).isoformat()
    assert tools.execute('estimate_cost',{'strategy':'basic'})['data']['feasible']
    assert tools.execute('search_materials',{})['ok']
    current[0]=NOW+timedelta(seconds=60)
    assert tools.execute('generate_script',{'strategy':'basic'})['ok']
    current[0]=NOW+timedelta(seconds=240)
    assert tools.execute('save_plan',{})['ok']
    assert tools.snapshot()['cost_total']==8
    tools.storage.close()


def test_multiple_blocking_reasons_preserve_tool_error_and_constraints(tmp_path):
    current=[NOW]
    r,_=validate_request(request('replan')|{'deadline':(NOW+timedelta(seconds=3000)).isoformat()})
    store=Storage(str(tmp_path/'multiple.sqlite'))
    store.bind(r,'mock')
    tools=Tools(r,store,clock=lambda:current[0],inject_failure='save_plan',failure_mode='always')
    run(tools)
    current[0]=NOW+timedelta(seconds=3500)
    tools.execute('estimate_cost',{'strategy':'premium'})
    result=Agent(tools,None,Trace('mock','test','multi')).result()
    reasons={r['code']:r for r in result['blocking_reasons']}
    assert result['status']=='budget_exceeded'
    assert reasons['BUDGET_EXCEEDED']['budget_gap']==8
    assert reasons['DEADLINE_EXCEEDED']['deadline_gap_seconds']==1100
    assert reasons['TOOL_FAILURE']['step_id']=='save'
    assert reasons['TOOL_FAILURE']['error']['code']=='INJECTED_TOOL_FAILURE'
    assert result['cost_total']==8
    tools.storage.close()


def test_superseded_failure_does_not_block_successful_alternative(tmp_path):
    tools=setup(tmp_path,failure='generate_script')
    run(tools)
    tools.inject_failure=None
    tools.execute('estimate_cost',{'strategy':'basic'})
    assert tools.execute('generate_script',{'strategy':'basic'})['ok']
    assert tools.execute('save_plan',{})['ok']
    result=Agent(tools,None,Trace('mock','test','alternative')).result()
    assert result['status']=='completed'
    assert result['blocking_reasons']==[]
    assert any(s['step_key']=='script:premium' and s['status']=='failed' for s in result['steps'])
    tools.storage.close()
