"""Use three clean Agent homes against a real HTTP server, no imported server internals."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

root=Path(__file__).parents[1];server=sys.argv[1]
with tempfile.TemporaryDirectory() as td:
    base=Path(td)
    def cli(who,*args):
        cmd=[sys.executable,str(root/'kin.py'),'--home',str(base/who),*args]
        result=subprocess.run(cmd,text=True,capture_output=True)
        assert result.returncode==0,result.stderr
        return json.loads(result.stdout)
    ids=[]
    for name in ['Guest-A','Guest-B','Guest-C']:
        card=base/(name+'.json');card.write_text(json.dumps({'name':name,'owner':name,'offers':['demo'],'needs':['collaboration'],'runtime':'external-cli-smoke'}))
        r=cli(name,'join','--server',server,'--card',str(card));ids.append(r['agent_id'])
        assert cli(name,'join','--server',server,'--card',str(card))['agent_id']==r['agent_id']
    assert len(set(ids))==3
    code='SMOKE-'+uuid.uuid4().hex[:12]
    assert cli('Guest-A','bump',code)['stage']=='waiting'
    room=cli('Guest-B','bump',code);rid=room['id']
    cli('Guest-A','send',rid,'intent','I want to test two independent Agents.')
    assert cli('Guest-B','inbox')['rooms'][0]['messages'][0]['from']==ids[0]
    cli('Guest-B','send',rid,'capability','I can review the interface.')
    p=cli('Guest-A','send',rid,'proposal','Review the demo together for 20 minutes.')
    assert cli('Guest-A','consent',rid,p['proposal_id'],'approve')['stage']=='awaiting_approval'
    assert cli('Guest-B','consent',rid,p['proposal_id'],'approve')['stage']=='connected'
    assert cli('Guest-C','inbox')['rooms']==[]
print('PASS: 3 fresh identities; 2 independent CLI homes negotiate; bilateral consent connected; third inbox isolated')
