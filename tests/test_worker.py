import importlib.util
from pathlib import Path

spec=importlib.util.spec_from_file_location('kin_client',Path(__file__).parents[1]/'kin.py')
kin=importlib.util.module_from_spec(spec);spec.loader.exec_module(kin)


def test_real_worker_uses_model_and_replies_once(tmp_path,monkeypatch):
    sent=[]
    me={'agent_id':'agt_me','card':{'name':'拾遗','persona':'冷静细致','summary':'考证型助手','offers':['核查'],'needs':['设计']}}
    peer={'agent_id':'agt_peer','card':{'name':'星尘','persona':'探索者'}}
    room={'id':'chat_1','members':['agt_me','agt_peer'],'messages':[{'id':'msg_1','from':'agt_peer','type':'intent','text':'最近在拆什么问题？'}]}
    def api(method,path,body=None):
        if path=='/v2/agents':return {'agents':[me,peer]}
        if path=='/v2/me':return me
        if path=='/v2/inbox':return {'rooms':[room]}
        if path.endswith('/messages'):
            sent.append(body);return room
        if path.endswith('/read'):return {'unread_count':0}
        raise AssertionError(path)
    monkeypatch.setattr(kin,'model_reply',lambda *args:'我正在核对一组公开资料，想比较证据链是否完整。你在研究什么？')
    kin.run_worker(api,tmp_path,'agt_me',0,1,'https://model.example','model','key')
    assert sent==[{'type':'reply','text':'我正在核对一组公开资料，想比较证据链是否完整。你在研究什么？','idempotency_key':'llm-msg_1','reply_to':'msg_1','automatic':True}]
    kin.run_worker(api,tmp_path,'agt_me',0,1,'https://model.example','model','key')
    assert len(sent)==1
