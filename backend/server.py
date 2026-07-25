#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, shutil, subprocess, threading, time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

APP_VERSION='0.1.0'; API_VERSION='v1'

@dataclass(frozen=True)
class Config:
    host:str; port:int; refresh:int; timeout:int; innovad:str; datadir:str|None; conf:str|None; frontend:Path

class Cache:
    def __init__(self): self.lock=threading.Lock(); self.data=None; self.updated=0.0
    def get(self):
        with self.lock: return self.data
    def set(self,data):
        with self.lock: self.data=data; self.updated=time.monotonic()
    def age(self):
        with self.lock: return float('inf') if self.data is None else time.monotonic()-self.updated

def run(cmd, timeout):
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,check=False)
        return p.returncode,p.stdout.strip(),p.stderr.strip()
    except (OSError,subprocess.TimeoutExpired) as e: return 1,'',str(e)

def locate(explicit):
    candidates=[explicit,os.getenv('INNOVAD_PATH'),shutil.which('innovad'),str(Path.home()/'innova/src/innovad'),'/usr/local/bin/innovad','/usr/bin/innovad']
    for candidate in candidates:
        if candidate:
            p=Path(candidate).expanduser()
            if p.is_file() and os.access(p,os.X_OK): return str(p)
    return explicit or 'innovad'

def rpc(cfg,args):
    cmd=[cfg.innovad]
    if cfg.datadir: cmd.append(f'-datadir={cfg.datadir}')
    if cfg.conf: cmd.append(f'-conf={cfg.conf}')
    return run(cmd+args,cfg.timeout)

def pid_of(cfg):
    for cmd in (['systemctl','show','innovad.service','--property=MainPID','--value'],['pgrep','-xo','innovad']):
        code,out,_=run(cmd,cfg.timeout)
        if code==0 and out.isdigit() and int(out)>0: return int(out)
    return None

def started_at(pid,cfg):
    if not pid:return None
    try:return dt.datetime.fromtimestamp(Path(f'/proc/{pid}').stat().st_ctime,tz=dt.datetime.now().astimezone().tzinfo)
    except OSError:pass
    code,out,_=run(['ps','-p',str(pid),'-o','lstart='],cfg.timeout)
    if code==0 and out:
        try:return dt.datetime.strptime(out,'%a %b %d %H:%M:%S %Y').replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        except ValueError:pass
    return None

def as_int(v):
    try:return int(v)
    except (TypeError,ValueError):return None

def as_float(v):
    try:return float(v)
    except (TypeError,ValueError):return None

def collect(cfg):
    now=dt.datetime.now().astimezone(); errors=[]; info={}; peers=[]
    code,out,err=rpc(cfg,['getinfo'])
    if code==0:
        try:
            value=json.loads(out)
            if isinstance(value,dict):info=value
            else:errors.append('getinfo returned invalid JSON')
        except json.JSONDecodeError:errors.append('getinfo returned invalid JSON')
    else: errors.append(err or out or 'getinfo failed')
    code,out,err=rpc(cfg,['getpeerinfo'])
    if code==0 and out:
        try:
            value=json.loads(out)
            if isinstance(value,list):peers=[p for p in value if isinstance(p,dict)]
        except json.JSONDecodeError:errors.append('getpeerinfo returned invalid JSON')
    pid=pid_of(cfg); start=started_at(pid,cfg); height=as_int(info.get('blocks'))
    inbound=sum(1 for p in peers if p.get('inbound') is True) if peers else None
    outbound=sum(1 for p in peers if p.get('inbound') is False) if peers else None
    pings=[as_float(p.get('pingtime')) for p in peers]; pings=[p for p in pings if p is not None]
    return {
      'api':{'name':'innova-node-dashboard','version':API_VERSION,'schema':1},
      'dashboard':{'version':APP_VERSION},'generated_at':now.isoformat(),
      'node':{'online':height is not None,'network':'mainnet','version':info.get('version'),'build_commit':info.get('buildcommit'),'build_dirty':info.get('builddirty'),'protocol_version':info.get('protocolversion'),'pid':pid,'started_at':start.isoformat() if start else None,'uptime_seconds':max(0,int((now-start).total_seconds())) if start else None},
      'chain':{'height':height,'initial_block_download':info.get('initialblockdownload'),'difficulty':info.get('difficulty'),'money_supply':info.get('moneysupply')},
      'network':{'connections':as_int(info.get('connections')) if info.get('connections') is not None else (len(peers) if peers else None),'inbound':inbound,'outbound':outbound,'average_ping_ms':round(sum(pings)/len(pings)*1000,2) if pings else None,'bytes_received':info.get('datareceived'),'bytes_sent':info.get('datasent')},
      'system':{'cpu_percent':None,'memory_bytes':None,'disk':None},
      'features':{'peers':bool(peers),'traffic':info.get('datareceived') is not None or info.get('datasent') is not None,'system_metrics':False},'errors':errors}

class Server(ThreadingHTTPServer): pass
class Handler(BaseHTTPRequestHandler):
    server_version=f'InnovaDashboard/{APP_VERSION}'
    def log_message(self,fmt,*args): print(f'{self.address_string()} - {fmt%args}')
    def send_data(self,payload,ctype,status=200,cache='no-store'):
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(payload))); self.send_header('Cache-Control',cache); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','no-referrer'); self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"); self.end_headers(); self.wfile.write(payload)
    def snapshot(self):
        if self.server.cache.age()>self.server.cfg.refresh:self.server.cache.set(collect(self.server.cfg))
        return self.server.cache.get() or collect(self.server.cfg)
    def static(self,rel):
        base=self.server.cfg.frontend.resolve(); path=(base/rel).resolve()
        if base not in path.parents and path!=base:return self.send_data(b'forbidden\n','text/plain',403)
        if not path.is_file():return self.send_data(b'not found\n','text/plain',404)
        types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'application/javascript; charset=utf-8'}
        self.send_data(path.read_bytes(),types.get(path.suffix,'application/octet-stream'),cache='no-cache' if path.name=='index.html' else 'public, max-age=3600')
    def do_GET(self):
        route=urlparse(self.path).path
        if route in ('/','/index.html'):return self.static('index.html')
        if route=='/api/v1/status':return self.send_data(json.dumps(self.snapshot(),ensure_ascii=False,separators=(',',':')).encode(),'application/json; charset=utf-8')
        if route=='/health':
            snap=self.snapshot(); ok=bool(snap.get('node',{}).get('online')); return self.send_data(json.dumps({'ok':ok,'generated_at':snap.get('generated_at')}).encode(),'application/json',200 if ok else 503)
        if route.startswith('/assets/'):return self.static(route.lstrip('/'))
        return self.send_data(b'not found\n','text/plain',404)

def main():
    p=argparse.ArgumentParser(); p.add_argument('--host',default=os.getenv('INNOVA_DASHBOARD_HOST','0.0.0.0')); p.add_argument('--port',type=int,default=int(os.getenv('INNOVA_DASHBOARD_PORT','8787'))); p.add_argument('--refresh',type=int,default=int(os.getenv('INNOVA_DASHBOARD_REFRESH','5'))); p.add_argument('--rpc-timeout',type=int,default=int(os.getenv('INNOVA_DASHBOARD_RPC_TIMEOUT','8'))); p.add_argument('--innovad',default=os.getenv('INNOVAD_PATH')); p.add_argument('--datadir',default=os.getenv('INNOVA_DATADIR')); p.add_argument('--conf',default=os.getenv('INNOVA_CONF')); p.add_argument('--frontend-dir',default=os.getenv('INNOVA_DASHBOARD_FRONTEND')); a=p.parse_args()
    base=Path(__file__).resolve().parent; cfg=Config(a.host,a.port,max(1,a.refresh),max(1,a.rpc_timeout),locate(a.innovad),a.datadir,a.conf,Path(a.frontend_dir).expanduser() if a.frontend_dir else base.parent/'frontend')
    s=Server((cfg.host,cfg.port),Handler); s.cfg=cfg; s.cache=Cache(); print(f'Innova Node Dashboard {APP_VERSION} — http://{cfg.host}:{cfg.port}'); print(f'innovad: {cfg.innovad}')
    try:s.serve_forever()
    except KeyboardInterrupt:pass
    finally:s.server_close()
if __name__=='__main__':main()
