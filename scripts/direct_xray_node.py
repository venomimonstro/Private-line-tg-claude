#!/usr/bin/env python3
from __future__ import annotations
import argparse, base64, fcntl, json, os, re, socket, subprocess, sys, tempfile, time
from pathlib import Path
from urllib.parse import quote, urlencode

ROOT=Path("/opt/private-line-node")
DATA=Path("/var/lib/private-line-node")
STATE=DATA/"state.json"
CONFIG=DATA/"xray.json"
LOCK=DATA/"lock"
XRAY=ROOT/"xray"
SERVICE="private-line-xray"
NAME_RE=re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UUID_RE=re.compile(r"^[0-9a-fA-F-]{36}$")

def die(msg, code=2):
    print(json.dumps({"ok":False,"error":msg},separators=(",",":")))
    raise SystemExit(code)

def run(cmd, check=True, timeout=30):
    p=subprocess.run(cmd,text=True,capture_output=True,timeout=timeout)
    if check and p.returncode:
        raise RuntimeError((p.stderr or p.stdout or "command_failed")[-1200:])
    return p

def load():
    if not STATE.is_file(): die("STATE_MISSING")
    try: s=json.loads(STATE.read_text())
    except Exception: die("STATE_INVALID")
    for k in ("host","port","sni","target","private_key","public_key","short_id","users"):
        if k not in s: die("STATE_INVALID")
    return s

def render(s):
    clients=[]
    for name,uid in sorted(s["users"].items()):
        if not NAME_RE.fullmatch(name) or not UUID_RE.fullmatch(uid):
            raise ValueError("INVALID_USER_STATE")
        clients.append({"id":uid,"email":name,"flow":"xtls-rprx-vision"})
    return {
      "log":{"loglevel":"warning"},
      "inbounds":[{
        "tag":"vless-reality","listen":"0.0.0.0","port":int(s["port"]),
        "protocol":"vless",
        "settings":{"clients":clients,"decryption":"none"},
        "sniffing":{"enabled":False},
        "streamSettings":{
          "network":"tcp","security":"reality","tcpSettings":{},
          "realitySettings":{
            "show":False,"target":s["target"],"xver":0,
            "serverNames":[s["sni"]],"privateKey":s["private_key"],
            "shortIds":[s["short_id"]],"minClientVer":"","maxClientVer":"",
            "maxTimeDiff":0
          }
        }
      }],
      "outbounds":[
        {"tag":"direct","protocol":"freedom","settings":{}},
        {"tag":"block","protocol":"blackhole","settings":{}}
      ],
      "routing":{"domainStrategy":"AsIs","rules":[
        {"type":"field","ip":["10.0.0.0/8","172.16.0.0/12","192.168.0.0/16","127.0.0.0/8","169.254.0.0/16","::1/128","fc00::/7","fe80::/10"],"outboundTag":"block"},
        {"type":"field","network":"tcp,udp","outboundTag":"direct"}
      ]}
    }

def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=path.name+".",dir=str(path.parent))
    try:
        with os.fdopen(fd,"w") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass

def port_ok(port):
    try:
        with socket.create_connection(("127.0.0.1",int(port)),timeout=1.5): return True
    except OSError: return False

def validate_candidate(candidate):
    p=run([str(XRAY),"run","-test","-config",str(candidate)],check=False,timeout=15)
    if p.returncode: raise RuntimeError("XRAY_CONFIG_INVALID")

def apply_state(old, new):
    cfg=json.dumps(render(new),indent=2)+"\n"
    candidate=DATA/"xray.candidate.json"
    atomic(candidate,cfg)
    validate_candidate(candidate)
    old_cfg=CONFIG.read_text() if CONFIG.exists() else None
    old_state=STATE.read_text() if STATE.exists() else None
    try:
        atomic(CONFIG,cfg)
        atomic(STATE,json.dumps(new,indent=2)+"\n")
        run(["systemctl","restart",SERVICE],timeout=20)
        ok=False
        for _ in range(30):
            if port_ok(new["port"]): ok=True; break
            time.sleep(.25)
        if not ok: raise RuntimeError("XRAY_LISTENER_NOT_READY")
    except Exception:
        if old_cfg is not None: atomic(CONFIG,old_cfg)
        if old_state is not None: atomic(STATE,old_state)
        run(["systemctl","restart",SERVICE],check=False,timeout=20)
        raise
    finally:
        try: candidate.unlink()
        except FileNotFoundError: pass

def info(s, name=None):
    d={"ok":True,"host":s["host"],"port":s["port"],"sni":s["sni"],
       "public_key":s["public_key"],"short_id":s["short_id"],
       "users":len(s["users"])}
    if name:
        uid=s["users"].get(name)
        if uid:
            d["name"]=name; d["uuid"]=uid
            q=urlencode({"encryption":"none","security":"reality","sni":s["sni"],
                         "fp":"chrome","pbk":s["public_key"],"sid":s["short_id"],
                         "type":"tcp","headerType":"none","flow":"xtls-rprx-vision"})
            d["url"]=f"vless://{uid}@{s['host']}:{s['port']}?{q}#"+quote("Private Line VPN")
    return d

def main():
    ap=argparse.ArgumentParser()
    sp=ap.add_subparsers(dest="cmd",required=True)
    for cmd in ("status","get","add","remove","sync"):
        p=sp.add_parser(cmd)
        if cmd in ("get","remove"): p.add_argument("name")
        if cmd=="add": p.add_argument("name"); p.add_argument("uuid")
        if cmd=="sync": p.add_argument("payload_b64")
    a=ap.parse_args()
    DATA.mkdir(parents=True,exist_ok=True)
    with LOCK.open("a+") as lf:
        fcntl.flock(lf,fcntl.LOCK_EX)
        s=load()
        if a.cmd=="status":
            p=run(["systemctl","is-active","--quiet",SERVICE],check=False)
            out=info(s); out["service_active"]=p.returncode==0; out["listener"]=port_ok(s["port"])
            print(json.dumps(out,separators=(",",":"))); return
        if a.cmd=="get":
            if not NAME_RE.fullmatch(a.name) or a.name not in s["users"]: die("USER_NOT_FOUND",3)
            print(json.dumps(info(s,a.name),separators=(",",":"))); return
        new=json.loads(json.dumps(s))
        if a.cmd=="add":
            if not NAME_RE.fullmatch(a.name) or not UUID_RE.fullmatch(a.uuid): die("INVALID_USER")
            new["users"][a.name]=a.uuid.lower()
        elif a.cmd=="remove":
            if not NAME_RE.fullmatch(a.name): die("INVALID_USER")
            new["users"].pop(a.name,None)
        elif a.cmd=="sync":
            try:
                raw=base64.urlsafe_b64decode(a.payload_b64.encode()+b"===")
                users=json.loads(raw)
            except Exception: die("INVALID_SYNC_PAYLOAD")
            if not isinstance(users,dict): die("INVALID_SYNC_PAYLOAD")
            cleaned={}
            for name,uid in users.items():
                if not isinstance(name,str) or not isinstance(uid,str) or not NAME_RE.fullmatch(name) or not UUID_RE.fullmatch(uid):
                    die("INVALID_SYNC_USER")
                cleaned[name]=uid.lower()
            new["users"]=cleaned
        apply_state(s,new)
        name=a.name if a.cmd=="add" else None
        print(json.dumps(info(new,name),separators=(",",":")))

if __name__=="__main__":
    try: main()
    except Exception as exc: die(type(exc).__name__+":"+str(exc)[:160])
