import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from test_record_queries import fixture, pod_lifecycle
from holmes_tools import record_pages


def test_byte_pages_preserve_all_containers_and_source_continuation(tmp_path, monkeypatch):
    monkeypatch.setenv('AIOPS_RECORD_CURSOR_DB', str(tmp_path/'pages.db'))
    monkeypatch.setenv('AIOPS_RECORD_RESULT_MAX_BYTES', '3500')
    payload = fixture()
    payload['items'] = [copy.deepcopy(payload['items'][0]) for _ in range(30)]
    for i, pod in enumerate(payload['items']):
        pod['metadata'].update(name=f'p-{i}', uid=f'u-{i}')
    args = {'record_type': 'container', 'page_size': 100, 'fields': ['ready','state','last_state']}
    with patch.object(pod_lifecycle.subprocess, 'run', return_value=SimpleNamespace(returncode=0,stdout=json.dumps(payload),stderr='')) as run:
        first = json.loads(pod_lifecycle.query(args))
    assert run.call_count == 1
    rows, page, tokens = [], first, []
    while True:
        assert page['status'] == 'success'
        assert len(json.dumps(page,ensure_ascii=False,separators=(',',':')).encode()) <= 3500
        rows.extend(page['rows'])
        token = page['pagination']['next_continue_token']
        if not token.startswith(record_pages.PREFIX):
            assert token == 'next'
            break
        tokens.append(token)
        with patch.object(pod_lifecycle.subprocess, 'run') as run:
            page = json.loads(pod_lifecycle.query({**args,'continue_token':token}))
            run.assert_not_called()
        assert page['source_metadata']['resource_version'] == 'v1'
    assert len(rows) == 60
    assert len({(r['uid'],r['container']) for r in rows}) == 60
    assert json.loads(pod_lifecycle.query({**args,'continue_token':tokens[0]}))['pagination']['offset'] == first['pagination']['returned_rows']
    changed = json.loads(pod_lifecycle.query({**args,'continue_token':tokens[0],'namespace':'other'}))
    assert 'query_changed' in changed['error']
    with record_pages.connect() as db:
        db.execute('UPDATE pages SET created=0')
    assert 'cursor_expired' in json.loads(pod_lifecycle.query({**args,'continue_token':tokens[0]}))['error']


def test_pod_ready_and_compact_defaults_are_facts():
    payload=fixture()
    payload['items'][0]['status']['conditions']=[{'type':'Ready','status':'False','reason':'ProbeFailed','lastTransitionTime':'2026-01-01T00:00:00Z'}]
    with patch.object(pod_lifecycle.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(payload),stderr='')):
        result=json.loads(pod_lifecycle.query({'record_type':'pod'}))
        grouped=json.loads(pod_lifecycle.query({'record_type':'pod','group_by':'ready'}))
    assert result['rows'][0]['ready'] is False
    assert result['rows'][0]['condition_states']=={'Ready':{'status':'False','reason':'ProbeFailed'}}
    assert 'conditions' not in result['columns']
    assert grouped['groups']==[{'ready':False,'count':1}]


def test_group_pages_keep_counts_and_last_page_not_partial(tmp_path, monkeypatch):
    monkeypatch.setenv('AIOPS_RECORD_CURSOR_DB',str(tmp_path/'pages.db'))
    packet={'contract_version':'evidence-records.v1','status':'success','coverage':'present',
            'source_metadata':{'next_continue_token':None},'result_type':'groups',
            'groups':[{'node':'node-'+str(i)+'x'*50,'count':2} for i in range(50)],'matched_rows':100}
    page=record_pages.paginate(packet,{'group_by':'node'},1200)
    groups=[]
    while True:
        groups.extend(page['groups'])
        token=page['pagination']['next_continue_token']
        if token is None: break
        page=record_pages.read({'group_by':'node','continue_token':token},1200)
    assert groups==packet['groups']
    assert page['coverage']=='partial'  # Last fragment is still not the entire source page.
