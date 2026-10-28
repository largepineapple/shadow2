import json
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self, mode, model, request_id):
        self.value = {'case_id':request_id,'mode':mode,'model':model,'started_at':datetime.now(timezone.utc).isoformat(),'steps':[],'published':False}

    def add(self, action, arguments, observation, basis=''):
        steps = self.value['steps']
        if steps: steps[-1]['next_action'] = action
        steps.append({'step':len(steps)+1,'action':action,'arguments':arguments,'observation_summary':observation,'next_action':None,'decision_basis':basis})

    def save(self, path, result):
        self.value.update(final_status=result['status'],finished_at=datetime.now(timezone.utc).isoformat(),result=result)
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        Path(path).write_text(json.dumps(self.value,ensure_ascii=False,indent=2),encoding='utf-8')
