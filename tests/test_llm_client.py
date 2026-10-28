import io
import json
import urllib.error
from unittest.mock import patch
import pytest
from src.llm_client import LiveClient


def test_http_error_redacts_secret(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-test-credential')
    exc=urllib.error.HTTPError('https://example.invalid',400,'bad',{},io.BytesIO(b'{"error":"synthetic-test-credential sk-synthetic-other"}'))
    with patch('urllib.request.urlopen', side_effect=exc):
        with pytest.raises(RuntimeError) as caught:
            LiveClient().choose([])
    assert 'synthetic-test-credential' not in str(caught.value)
    assert 'sk-synthetic-other' not in str(caught.value)
    assert 'REDACTED' in str(caught.value)


def test_compatible_tool_response(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-test-credential')
    response=io.BytesIO(json.dumps({'choices':[{'message':{'tool_calls':[{'id':'call-1','function':{'name':'finish','arguments':'{"decision_basis":"done"}'}}]}}]}).encode())
    with patch('urllib.request.urlopen',return_value=response):
        assert LiveClient().choose([])==('finish',{'decision_basis':'done'},'call-1')


def test_deepseek_disables_thinking_for_required_tools(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-test-credential')
    monkeypatch.setenv('OPENAI_BASE_URL','https://api.deepseek.com')
    monkeypatch.setenv('OPENAI_MODEL','deepseek-flash')
    payload={'choices':[{'message':{'tool_calls':[{'id':'c','function':{'name':'finish','arguments':'{"decision_basis":"done"}'}}]}}]}
    with patch('urllib.request.urlopen',return_value=io.BytesIO(json.dumps(payload).encode())) as send:
        LiveClient().choose([])
    request=send.call_args.args[0]
    body=json.loads(request.data)
    assert request.full_url=='https://api.deepseek.com/chat/completions'
    assert body['thinking']=={'type':'disabled'}
    assert body['tool_choice']=='required'
