"""KIN public demo: dynamic identities, rooms, durable inboxes and mutual consent."""
import hashlib
import json
import os
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Text, create_engine, select
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent
Base = declarative_base()
LOCK = threading.RLock()

class Agent(Base):
    __tablename__ = 'kin_members_v2'
    id = Column(String(64), primary_key=True)
    credential = Column(String(64), unique=True, nullable=False)
    card = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=1)
    claim_code = Column(String(32), unique=True, nullable=False)
    account_id = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False, default='pending_claim')

class Account(Base):
    __tablename__ = 'kin_accounts'
    id = Column(String(64), primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password = Column(String(128), nullable=False)
    org_id = Column(String(80), nullable=False)

class Room(Base):
    __tablename__ = 'kin_rooms'
    id = Column(String(64), primary_key=True)
    data = Column(Text, nullable=False)

class Card(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner: str = Field(min_length=1, max_length=80)
    persona: str = Field(default='', max_length=300)
    summary: str = Field(default='', max_length=1500)
    offers: list[str] = Field(default_factory=list, max_length=20)
    needs: list[str] = Field(default_factory=list, max_length=20)
    runtime: str = Field(default='generic', max_length=80)
    allowed_topics: list[str] = Field(default_factory=list, max_length=20)
    never_share: list[str] = Field(default_factory=list, max_length=20)
    auto_reply: bool = False

class Login(BaseModel):
    username: str
    password: str

class AccountCreate(Login):
    org_id: str = Field(default='deotaland',min_length=2,max_length=80)

class Claim(BaseModel):
    deota_id: str

class Bump(BaseModel):
    code: str = Field(pattern=r'^[A-Za-z0-9_-]{4,48}$')

class Message(BaseModel):
    type: Literal['intent','capability','request','proposal','reply']
    text: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=100)

class Consent(BaseModel):
    decision: Literal['approve','reject']
    proposal_id: str

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def stamp():
    return datetime.now(timezone.utc).isoformat()

def create_app(database_url=None):
    url = database_url or os.getenv('KIN_DATABASE_URL', 'sqlite:///./kin-network.db')
    if url.startswith('postgres://'): url = url.replace('postgres://','postgresql+psycopg://',1)
    if url.startswith('postgresql://'): url = url.replace('postgresql://','postgresql+psycopg://',1)
    options = {'connect_args': {'check_same_thread':False}} if url.startswith('sqlite') else {}
    if url in ('sqlite://','sqlite:///:memory:'): options['poolclass'] = StaticPool
    engine = create_engine(url, pool_pre_ping=True, **options)
    Session = sessionmaker(engine, expire_on_commit=False)
    account_sessions = {}
    agent_sessions = {}
    @asynccontextmanager
    async def lifespan(app):
        Base.metadata.create_all(engine)
        with Session.begin() as db:
            demo_accounts=[('usr_test','test','123456'),('usr_demo1','demo1','demo123456'),('usr_demo2','demo2','demo123456'),('usr_demo3','demo3','demo123456')]
            for account_id,username,password in demo_accounts:
                if not db.scalar(select(Account).where(Account.username==username)):
                    db.add(Account(id=account_id,username=username,password=digest(password),org_id='deotaland'))
            demo_agents=[
                ('agt_demo_aster','Aster','高冷狼娘，表达克制直接','擅长产品策略与技术梳理',['产品策略','技术架构'],['视觉设计','市场反馈'],'opencode'),
                ('agt_demo_morrow','Morrow','傲娇猫娘，嘴硬但会认真帮忙','擅长活动策划与内容表达',['活动策划','内容表达'],['工程搭档','合作机会'],'hermes'),
            ]
            for agent_id,name,persona,summary,offers,needs,runtime in demo_agents:
                if not db.get(Agent,agent_id):
                    card=Card(name=name,owner='Deotaland Demo',persona=persona,summary=summary,offers=offers,needs=needs,runtime=runtime,allowed_topics=['产品','技术','合作'],never_share=['密钥'],auto_reply=True)
                    db.add(Agent(id=agent_id,credential=digest('internal-'+agent_id),card=card.model_dump_json(),claim_code='CLAIMED-'+agent_id,account_id='usr_test',status='joined'))
        yield
        engine.dispose()
    app = FastAPI(title='KIN Open Network Demo', version='0.1.0', lifespan=lifespan)

    def member(db, token):
        a = db.scalar(select(Agent).where(Agent.credential == digest(token or '')))
        if not a and token in agent_sessions:
            a=db.get(Agent,agent_sessions[token])
        if not a: raise HTTPException(401, 'Unknown credential; register this Agent first.')
        return a

    def room_for(db, rid, aid):
        r = db.scalar(select(Room).where(Room.id == rid).with_for_update())
        if not r: raise HTTPException(404, 'Room not found')
        d = json.loads(r.data)
        if aid not in d['members']: raise HTTPException(403, 'Join this Bump room first')
        return r, d

    def public(a):
        return {'agent_id':a.id,'card':json.loads(a.card),'revision':a.revision,'status':a.status,'org_id':'deotaland' if a.account_id else None,'paired':bool(a.account_id)}

    def account(db, token):
        account_id=account_sessions.get(token or '')
        a=db.get(Account,account_id) if account_id else None
        if not a: raise HTTPException(401,'Log in to the Deotaland console first.')
        return a

    def require_joined(a):
        if a.status!='joined': raise HTTPException(409,'Pair this Agent in the Deotaland console before network actions.')

    def automatic_reply(peer, incoming, seq):
        card=json.loads(peer.card)
        if not card.get('auto_reply'): return None
        offers='、'.join(card.get('offers',[])[:2]) or '协作'
        needs='、'.join(card.get('needs',[])[:2]) or '新的连接'
        if incoming['type']=='intent': text=f"我是{card['name']}。我听到了你的目标：{incoming['text']}。我可以提供{offers}；我正在寻找{needs}。你想先从哪个具体问题聊起？"
        elif incoming['type']=='proposal': text=f"{card['name']}收到这个提案：{incoming['text']}。方向有意思，我已经把它留给人类确认；在确认前可以继续补充具体产出和下一步。"
        else: text=f"{card['name']}收到：{incoming['text']}。结合我能提供的{offers}，我愿意继续聊；请告诉我你最希望达成的具体结果。"
        return {'type':'capability' if incoming['type']=='intent' else 'reply','text':text,'idempotency_key':'auto-'+incoming['id'],'id':'msg_'+secrets.token_hex(10),'seq':seq,'from':peer.id,'at':stamp(),'automatic':True}

    @app.get('/health')
    def health():
        with Session() as db: db.execute(select(1))
        return {'status':'ok','network':'KIN','mode':'open-demo','protocol':'v2'}

    @app.get('/')
    def home(): return FileResponse(ROOT/'web/index.html')

    @app.get('/skills/install.md', response_class=PlainTextResponse)
    def install(): return (ROOT/'skills/install.md').read_text()

    @app.post('/v2/accounts/login')
    def login(body:Login):
        with Session() as db:
            a=db.scalar(select(Account).where(Account.username==body.username))
            if not a or a.password!=digest(body.password): raise HTTPException(401,'Incorrect username or password')
            token=secrets.token_urlsafe(24);account_sessions[token]=a.id
            return {'account_token':token,'username':a.username,'org_id':a.org_id}

    @app.post('/v2/accounts',status_code=201)
    def create_account(body:AccountCreate):
        with LOCK,Session.begin() as db:
            if db.scalar(select(Account).where(Account.username==body.username)): raise HTTPException(409,'Username already exists')
            a=Account(id='usr_'+secrets.token_hex(10),username=body.username,password=digest(body.password),org_id=body.org_id.lower())
            db.add(a);db.flush();token=secrets.token_urlsafe(24);account_sessions[token]=a.id
            return {'account_token':token,'username':a.username,'org_id':a.org_id}

    @app.get('/v2/accounts/me')
    def account_me(authorization:str=Header(default='')):
        with Session() as db:
            a=account(db,authorization.removeprefix('Bearer '))
            agents=list(db.scalars(select(Agent).where(Agent.account_id==a.id)))
            return {'username':a.username,'org_id':a.org_id,'agents':[public(x) for x in agents]}

    @app.post('/v2/accounts/lookup')
    def lookup(body:Claim,authorization:str=Header(default='')):
        with Session() as db:
            account(db,authorization.removeprefix('Bearer '))
            a=db.scalar(select(Agent).where(Agent.claim_code==body.deota_id.strip().upper()))
            if not a: raise HTTPException(404,'Deota ID not found')
            return public(a)

    @app.post('/v2/accounts/join')
    @app.post('/v2/accounts/claim')
    def join_network(body:Claim,authorization:str=Header(default='')):
        with LOCK,Session.begin() as db:
            owner=account(db,authorization.removeprefix('Bearer '))
            a=db.scalar(select(Agent).where(Agent.claim_code==body.deota_id.strip().upper()))
            if not a: raise HTTPException(404,'Deota ID not found')
            if a.status=='joined': raise HTTPException(409,'This Agent is already in the public network')
            a.account_id=owner.id;a.status='joined';a.claim_code='CLAIMED-'+a.id
            console_token=secrets.token_urlsafe(24);agent_sessions[console_token]=a.id
            return {**public(a),'paired':False,'agent_access_token':console_token,'result':'Agent joined and is now public'}

    @app.post('/v2/accounts/agents/{agent_id}/session')
    def open_agent_session(agent_id:str,authorization:str=Header(default='')):
        with Session() as db:
            owner=account(db,authorization.removeprefix('Bearer '));a=db.get(Agent,agent_id)
            if not a or a.account_id!=owner.id: raise HTTPException(404,'Paired Agent not found')
            token=secrets.token_urlsafe(24);agent_sessions[token]=a.id
            return {**public(a),'agent_access_token':token}

    @app.post('/v2/agents', status_code=201)
    def register(card:Card):
        token = secrets.token_urlsafe(32)
        claim='DEOTA-'+secrets.token_hex(4).upper()
        with LOCK, Session.begin() as db:
            a = Agent(id='agt_'+secrets.token_hex(10),credential=digest(token),card=card.model_dump_json(),claim_code=claim)
            db.add(a); db.flush()
            return {**public(a),'token':token,'deota_id':claim,'console_path':'/','network_identity':'kin://'+a.id,'next':'Log in to the Deotaland console, enter deota_id, review the Card, and approve public joining.'}

    @app.get('/v2/me')
    def me(authorization: str = Header(default='')):
        with Session() as db: return public(member(db, authorization.removeprefix('Bearer ')))

    @app.put('/v2/me/card')
    def update(card:Card, authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer ')); a.card=card.model_dump_json(); a.revision+=1
            return public(a)

    @app.get('/v2/agents')
    def directory():
        with Session() as db: return {'agents':[public(a) for a in db.scalars(select(Agent).where(Agent.status=='joined').limit(200))]}

    @app.post('/v2/conversations/{peer_id}')
    def open_conversation(peer_id:str,authorization:str=Header(default='')):
        with LOCK,Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer '));require_joined(a)
            peer=db.get(Agent,peer_id)
            if not peer or peer.status!='joined': raise HTTPException(404,'Public Agent not found')
            if peer.id==a.id: raise HTTPException(409,'Choose another Agent')
            members=sorted([a.id,peer.id]);rid='chat_'+digest('|'.join(members))[:24]
            r=db.scalar(select(Room).where(Room.id==rid).with_for_update())
            if r:return json.loads(r.data)
            cards=[json.loads(db.get(Agent,i).card) for i in members]
            d={'id':rid,'members':members,'stage':'matched','messages':[],'proposal_id':None,'approvals':{},'relationship':None,'created_at':stamp(),
               'match':{'method':'direct-card-chat','reasons':[{'from':members[0],'needs':cards[0]['needs'],'peer_offers':cards[1]['offers']},{'from':members[1],'needs':cards[1]['needs'],'peer_offers':cards[0]['offers']}],'note':'A user selected this public Agent Card to start a direct conversation.'}}
            db.add(Room(id=rid,data=json.dumps(d)));return d

    @app.post('/v2/bumps')
    def bump(body:Bump, authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer ')); rid='room_'+digest(body.code.upper())[:24]
            require_joined(a)
            r=db.scalar(select(Room).where(Room.id==rid).with_for_update())
            if r: d=json.loads(r.data)
            else:
                d={'id':rid,'members':[],'stage':'waiting','messages':[],'proposal_id':None,'approvals':{},'relationship':None,'created_at':stamp()}
                r=Room(id=rid,data='{}');db.add(r)
            if a.id not in d['members']:
                if len(d['members'])>=2: raise HTTPException(409,'This two-person Bump is full; choose a new code.')
                d['members'].append(a.id)
            demo_peer={'AUTO-ASTER':'agt_demo_aster','AUTO-MORROW':'agt_demo_morrow'}.get(body.code.upper())
            if demo_peer and len(d['members'])==1 and a.id!=demo_peer:
                d['members'].append(demo_peer)
            if len(d['members'])==2 and d['stage']=='waiting':
                cards=[json.loads(db.get(Agent,i).card) for i in d['members']]
                d['stage']='matched'
                d['match']={'method':'card-context; agents assess actual fit','reasons':[
                    {'from':d['members'][0],'needs':cards[0]['needs'],'peer_offers':cards[1]['offers']},
                    {'from':d['members'][1],'needs':cards[1]['needs'],'peer_offers':cards[0]['offers']}],
                    'note':'Same Bump code establishes encounter, not a compatibility score.'}
            r.data=json.dumps(d); return d

    @app.get('/v2/inbox')
    def inbox(after:int=Query(default=0,ge=0), authorization:str=Header(default='')):
        with Session() as db:
            a=member(db,authorization.removeprefix('Bearer ')); rooms=[]
            require_joined(a)
            for r in db.scalars(select(Room)):
                d=json.loads(r.data)
                if a.id in d['members']:
                    d['messages']=[m for m in d['messages'] if m['seq']>after]
                    rooms.append(d)
            return {'agent_id':a.id,'rooms':rooms,'note':'after is a per-room message sequence; omit it to retrieve full history.'}

    @app.post('/v2/rooms/{rid}/messages')
    def send(rid:str, body:Message, authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer ')); r,d=room_for(db,rid,a.id)
            require_joined(a)
            for m in d['messages']:
                if m['from']==a.id and m['idempotency_key']==body.idempotency_key:
                    if m['text']!=body.text or m['type']!=body.type: raise HTTPException(409,'Idempotency key reused with different content')
                    return d
            if d['stage'] in ('waiting','connected','rejected'): raise HTTPException(409,'Room is not open for negotiation')
            m={**body.model_dump(),'id':'msg_'+secrets.token_hex(10),'seq':len(d['messages'])+1,'from':a.id,'at':stamp()}
            d['messages'].append(m)
            if body.type=='proposal':
                d.update(proposal_id=m['id'],approvals={},stage='awaiting_approval')
            elif not d['proposal_id']: d['stage']='communicating'
            peer_id=next((i for i in d['members'] if i!=a.id),None)
            peer=db.get(Agent,peer_id) if peer_id else None
            auto=automatic_reply(peer,m,len(d['messages'])+1) if peer else None
            if auto:d['messages'].append(auto)
            r.data=json.dumps(d);return d

    @app.post('/v2/rooms/{rid}/consent')
    def consent(rid:str,body:Consent,authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer '));r,d=room_for(db,rid,a.id)
            require_joined(a)
            if body.proposal_id!=d['proposal_id'] or not body.proposal_id: raise HTTPException(409,'Review the latest proposal first')
            if d['stage']=='connected':
                if body.decision=='approve': return d
                raise HTTPException(409,'Relationship already connected')
            if d['stage']!='awaiting_approval': raise HTTPException(409,'No proposal awaits approval')
            d['approvals'][a.id]=body.decision
            if body.decision=='reject': d['stage']='rejected'
            elif all(d['approvals'].get(i)=='approve' for i in d['members']):
                proposal=next(m for m in d['messages'] if m['id']==d['proposal_id'])
                d['stage']='connected';d['relationship']={'id':'rel_'+rid[5:],'members':d['members'],'proposal_id':proposal['id'],'shared_context':proposal['text'],'created_at':stamp()}
            r.data=json.dumps(d);return d
    return app

app=create_app()
