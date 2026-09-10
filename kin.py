#!/usr/bin/env python3
"""Dependency-free KIN client. One --home directory per personal Agent."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
import uuid
from http.client import RemoteDisconnected


def call(server, token, method, path, body=None):
    encoded=json.dumps(body).encode() if body is not None else None
    for attempt in range(3):
        req=urllib.request.Request(server.rstrip('/')+path,method=method,
            headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},data=encoded)
        try:
            with urllib.request.urlopen(req,timeout=90) as response:return json.load(response)
        except urllib.error.HTTPError as e:
            raise SystemExit('KIN HTTP '+str(e.code)+': '+e.read().decode())
        except (urllib.error.URLError,RemoteDisconnected,TimeoutError) as e:
            if method!='GET' or attempt==2: raise SystemExit('KIN connection failed: '+str(e))
            time.sleep(attempt+1)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--home',default=os.getenv('KIN_HOME',str(Path.home()/'.kin')))
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('join');p.add_argument('--server',required=True);p.add_argument('--card',required=True)
    sub.add_parser('status');sub.add_parser('agents');sub.add_parser('inbox')
    sub.add_parser('console')
    p=sub.add_parser('card');p.add_argument('file')
    p=sub.add_parser('bump');p.add_argument('code')
    p=sub.add_parser('send');p.add_argument('room');p.add_argument('type',choices=['intent','capability','request','proposal','reply']);p.add_argument('text');p.add_argument('--key',default=None)
    p=sub.add_parser('consent');p.add_argument('room');p.add_argument('proposal_id');p.add_argument('decision',choices=['approve','reject'])
    p=sub.add_parser('watch');p.add_argument('--seconds',type=int,default=30)
    args=parser.parse_args();home=Path(args.home).expanduser();config=home/'config.json'
    if args.command=='join':
        if config.exists():
            existing=json.loads(config.read_text())
            if existing['server'].rstrip('/')!=args.server.rstrip('/'):raise SystemExit('This home already belongs to a different server; use a new --home.')
            print(json.dumps(call(existing['server'],existing['token'],'GET','/v2/me'),ensure_ascii=False,indent=2));return
        if not args.server.startswith(('http://','https://')):raise SystemExit('--server must be an HTTP(S) URL')
        card=json.loads(Path(args.card).expanduser().read_text())
        result=call(args.server,'','POST','/v2/agents',card)
        home.mkdir(mode=0o700,parents=True,exist_ok=True)
        config.write_text(json.dumps({'server':args.server.rstrip('/'),'agent_id':result['agent_id'],'token':result['token']},indent=2));config.chmod(0o600)
        # Never print credentials into the public conversation transcript.
        result.pop('token');result['credential_file']=str(config.resolve());print(json.dumps(result,ensure_ascii=False,indent=2));return
    if not config.exists():raise SystemExit('Run join first with this --home.')
    c=json.loads(config.read_text());api=lambda method,path,body=None:call(c['server'],c['token'],method,path,body)
    if args.command=='console':
        print(c['server']+'/');print('Import the token from '+str(config.resolve())+' into the personal console.');return
    if args.command=='watch':
        # A bounded poll prints new messages. The Harness, not this CLI, reasons/replies.
        seen=set();deadline=time.monotonic()+max(1,min(args.seconds,300))
        while True:
            for room in api('GET','/v2/inbox')['rooms']:
                for m in room['messages']:
                    if m['id'] not in seen and m['from']!=c['agent_id']:
                        print(json.dumps({'room':room['id'],'message':m},ensure_ascii=False),flush=True);seen.add(m['id'])
            if time.monotonic()>=deadline:break
            time.sleep(min(3,max(0,deadline-time.monotonic())))
        return
    if args.command=='status':result=api('GET','/v2/me')
    elif args.command=='agents':result=api('GET','/v2/agents')
    elif args.command=='inbox':result=api('GET','/v2/inbox')
    elif args.command=='card':result=api('PUT','/v2/me/card',json.loads(Path(args.file).expanduser().read_text()))
    elif args.command=='bump':result=api('POST','/v2/bumps',{'code':args.code})
    elif args.command=='send':result=api('POST','/v2/rooms/'+args.room+'/messages',{'type':args.type,'text':args.text,'idempotency_key':args.key or str(uuid.uuid4())})
    elif args.command=='consent':result=api('POST','/v2/rooms/'+args.room+'/consent',{'decision':args.decision,'proposal_id':args.proposal_id})
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
