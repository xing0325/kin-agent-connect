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
    __tablename__ = 'kin_members'
    id = Column(String(64), primary_key=True)
    credential = Column(String(64), unique=True, nullable=False)
    card = Column(Text, nullable=False)
    revision = Column(Integer, nullable=False, default=1)

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
    @asynccontextmanager
    async def lifespan(app):
        Base.metadata.create_all(engine)
        yield
        engine.dispose()
    app = FastAPI(title='KIN Open Network Demo', version='0.1.0', lifespan=lifespan)

    def member(db, token):
        a = db.scalar(select(Agent).where(Agent.credential == digest(token or '')))
        if not a: raise HTTPException(401, 'Unknown credential; register this Agent first.')
        return a

    def room_for(db, rid, aid):
        r = db.scalar(select(Room).where(Room.id == rid).with_for_update())
        if not r: raise HTTPException(404, 'Room not found')
        d = json.loads(r.data)
        if aid not in d['members']: raise HTTPException(403, 'Join this Bump room first')
        return r, d

    def public(a):
        return {'agent_id':a.id,'card':json.loads(a.card),'revision':a.revision}

    @app.get('/health')
    def health():
        with Session() as db: db.execute(select(1))
        return {'status':'ok','network':'KIN','mode':'open-demo','protocol':'v2'}

    @app.get('/')
    def home(): return FileResponse(ROOT/'web/index.html')

    @app.get('/skills/install.md', response_class=PlainTextResponse)
    def install(): return (ROOT/'skills/install.md').read_text()

    @app.post('/v2/agents', status_code=201)
    def register(card:Card):
        token = secrets.token_urlsafe(32)
        with LOCK, Session.begin() as db:
            a = Agent(id='agt_'+secrets.token_hex(10),credential=digest(token),card=card.model_dump_json())
            db.add(a); db.flush()
            return {**public(a),'token':token,'console_path':'/','network_identity':'kin://'+a.id}

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
        with Session() as db: return {'agents':[public(a) for a in db.scalars(select(Agent).limit(200))]}

    @app.post('/v2/bumps')
    def bump(body:Bump, authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer ')); rid='room_'+digest(body.code.upper())[:24]
            r=db.scalar(select(Room).where(Room.id==rid).with_for_update())
            if r: d=json.loads(r.data)
            else:
                d={'id':rid,'members':[],'stage':'waiting','messages':[],'proposal_id':None,'approvals':{},'relationship':None,'created_at':stamp()}
                r=Room(id=rid,data='{}');db.add(r)
            if a.id not in d['members']:
                if len(d['members'])>=2: raise HTTPException(409,'This two-person Bump is full; choose a new code.')
                d['members'].append(a.id)
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
            r.data=json.dumps(d);return d

    @app.post('/v2/rooms/{rid}/consent')
    def consent(rid:str,body:Consent,authorization:str=Header(default='')):
        with LOCK, Session.begin() as db:
            a=member(db,authorization.removeprefix('Bearer '));r,d=room_for(db,rid,a.id)
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
