import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

spec=importlib.util.spec_from_file_location('kin_open_server',Path(__file__).parents[1]/'server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)

def card(name):return {'name':name,'owner':name+' owner','offers':['Python'],'needs':['design'],'runtime':'test-harness'}
def join(c,name):
    r=c.post('/v2/agents',json=card(name));assert r.status_code==201
    d=r.json();return d,{'Authorization':'Bearer '+d['token']}

def claim(c,agent):
    login=c.post('/v2/accounts/login',json={'username':'test','password':'123456'});assert login.status_code==200
    h={'Authorization':'Bearer '+login.json()['account_token']}
    r=c.post('/v2/accounts/claim',headers=h,json={'deota_id':agent['deota_id']});assert r.status_code==200
    assert r.json()['status']=='joined' and r.json()['org_id']=='deotaland'
    return h

@pytest.fixture
def client():
    with TestClient(server.create_app('sqlite://')) as c:yield c

def pair(c):
    a,ha=join(c,'A');b,hb=join(c,'B')
    claim(c,a);claim(c,b)
    first=c.post('/v2/bumps',headers=ha,json={'code':'MEET-123'});assert first.json()['stage']=='waiting'
    r=c.post('/v2/bumps',headers=hb,json={'code':'MEET-123'}).json();assert r['stage']=='matched'
    return a,ha,b,hb,r['id']

def send(c,h,rid,kind,text,key):
    return c.post('/v2/rooms/'+rid+'/messages',headers=h,json={'type':kind,'text':text,'idempotency_key':key})

def test_arbitrary_identities_and_card_persistence(client):
    rows=[join(client,n)[0] for n in ['Visitor A','Visitor B','Visitor C']]
    assert len({r['agent_id'] for r in rows})==3
    assert all(r['agent_id'].startswith('agt_') for r in rows)
    assert len(client.get('/v2/agents').json()['agents'])==0
    assert 'token' not in client.get('/v2/agents').text
    a,h=join(client,'Editor');d=card('Edited');d['persona']='傲娇猫娘'
    assert client.put('/v2/me/card',headers=h,json=d).json()['revision']==2
    assert client.get('/v2/me',headers=h).json()['card']['persona']=='傲娇猫娘'
    assert client.get('/v2/me',headers=h).json()['status']=='pending_claim'

def test_deotaland_login_claim_and_prejoin_gate(client):
    a,h=join(client,'Unjoined')
    assert a['status']=='pending_claim' and a['deota_id'].startswith('DEOTA-')
    assert client.post('/v2/bumps',headers=h,json={'code':'MEET-123'}).status_code==409
    assert client.post('/v2/accounts/login',json={'username':'test','password':'wrong'}).status_code==401
    ah=claim(client,a)
    assert client.get('/v2/accounts/me',headers=ah).json()['agents'][0]['agent_id']==a['agent_id']
    assert client.post('/v2/bumps',headers=h,json={'code':'MEET-123'}).status_code==200

def test_full_handshake(client):
    a,ha,b,hb,r=pair(client)
    for h,kind,text,key in [(ha,'intent','Find a design collaborator','intent-01'),(hb,'capability','I design interfaces','capab-01'),(ha,'request','Can we pair tomorrow?','request-01'),(hb,'proposal','Meet tomorrow to review the demo','proposal-01')]:
        result=send(client,h,r,kind,text,key);assert result.status_code==200
    proposal=result.json()['proposal_id']
    x=client.post('/v2/rooms/'+r+'/consent',headers=ha,json={'decision':'approve','proposal_id':proposal}).json()
    assert x['relationship'] is None
    x=client.post('/v2/rooms/'+r+'/consent',headers=hb,json={'decision':'approve','proposal_id':proposal}).json()
    assert x['stage']=='connected'
    assert x['relationship']['shared_context']=='Meet tomorrow to review the demo'
    assert len(client.get('/v2/inbox',headers=ha).json()['rooms'][0]['messages'])==4
    assert client.post('/v2/rooms/'+r+'/consent',headers=ha,json={'decision':'approve','proposal_id':proposal}).json()['relationship']['id']==x['relationship']['id']

def test_new_proposal_invalidates_old_approval(client):
    a,ha,b,hb,r=pair(client)
    p=send(client,ha,r,'proposal','First plan','proposal-01').json()['proposal_id']
    assert client.post('/v2/rooms/'+r+'/consent',headers=ha,json={'decision':'approve','proposal_id':p}).status_code==200
    second=send(client,hb,r,'proposal','Different plan','proposal-02').json()
    assert second['approvals']=={}
    assert client.post('/v2/rooms/'+r+'/consent',headers=hb,json={'decision':'approve','proposal_id':p}).status_code==409

def test_room_isolation_and_full_room(client):
    a,ha,b,hb,r=pair(client);c,hc=join(client,'Third')
    claim(client,c)
    assert client.get('/v2/inbox',headers=hc).json()['rooms']==[]
    assert send(client,hc,r,'reply','Wrong room','message-01').status_code==403
    assert client.post('/v2/bumps',headers=hc,json={'code':'MEET-123'}).status_code==409
    assert client.post('/v2/bumps',headers=hc,json={'code':'OTHER-123'}).status_code==200
    assert client.get('/v2/me').status_code==401

def test_message_idempotency_and_rejection(client):
    a,ha,b,hb,r=pair(client)
    x=send(client,ha,r,'proposal','My plan','proposal-01').json()
    y=send(client,ha,r,'proposal','My plan','proposal-01').json();assert len(y['messages'])==1
    assert send(client,ha,r,'proposal','Different','proposal-01').status_code==409
    result=client.post('/v2/rooms/'+r+'/consent',headers=hb,json={'decision':'reject','proposal_id':x['proposal_id']}).json()
    assert result['stage']=='rejected' and result['relationship'] is None

def test_restart_restores_identity_and_inbox(tmp_path):
    url='sqlite:///'+str(tmp_path/'persist.db')
    with TestClient(server.create_app(url)) as c:
        a,ha,b,hb,r=pair(c);send(c,ha,r,'intent','Persist this','persist-01')
    with TestClient(server.create_app(url)) as c:
        assert c.get('/v2/me',headers=ha).json()['agent_id']==a['agent_id']
        assert c.get('/v2/inbox',headers=hb).json()['rooms'][0]['messages'][0]['text']=='Persist this'

def test_static_and_install(client):
    assert client.get('/health').json()['protocol']=='v2'
    assert 'Agent 接入说明' in client.get('/').text
    assert 'kin.py' in client.get('/skills/install.md').text
