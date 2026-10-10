from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

CAMERA_ID = re.compile(r"^bodycam(?:0[1-9]|[12][0-9]|3[0-3])$")
ROOT = Path(__file__).resolve().parent.parent


def configured_secret(name):
    if name in os.environ:
        return os.environ[name]
    config = Path(os.getenv("DATA_DIR", "/data")) / "config.json"
    if not config.exists():
        return ""
    return json.loads(config.read_text())[name]


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "/data")))
    admin_password: str = field(default_factory=lambda: configured_secret("ADMIN_PASSWORD"))
    viewer_password: str = field(default_factory=lambda: configured_secret("VIEWER_PASSWORD"))
    internal_key: str = field(default_factory=lambda: configured_secret("INTERNAL_KEY"))
    publish_host: str = field(default_factory=lambda: os.getenv("PUBLISH_HOST", "localhost"))
    public_api_url: str = field(default_factory=lambda: os.getenv("PUBLIC_API_URL", ""))
    media_api: str = field(default_factory=lambda: os.getenv("MEDIA_API", "http://mediamtx:9997"))
    media_hls: str = field(default_factory=lambda: os.getenv("MEDIA_HLS", "http://mediamtx:8888"))
    media_webrtc: str = field(default_factory=lambda: os.getenv("MEDIA_WEBRTC", "http://mediamtx:8889"))
    rtmp_port: int = field(default_factory=lambda: int(os.getenv("RTMP_PORT", "11935")))
    rtsp_port: int = field(default_factory=lambda: int(os.getenv("RTSP_PORT", "18554")))
    srt_port: int = field(default_factory=lambda: int(os.getenv("SRT_PORT", "18890")))
    poll_seconds: float = 3
    frontend_dir: Path = ROOT / "dist"
    cors_origins: list[str] = field(default_factory=lambda: [
        x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()])

    @property
    def recordings_dir(self):
        return self.data_dir / "recordings"


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256)


class CameraUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    group: str | None = Field(default=None, max_length=80)
    officer: str | None = Field(default=None, max_length=80)
    model: str | None = Field(default=None, max_length=100)
    mode: Literal["publish", "rtsp"] | None = None
    rtsp_url: str | None = Field(default=None, max_length=4096)
    enabled: bool | None = None
    recording_enabled: bool | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.rtsp_url:
            u = urlparse(self.rtsp_url)
            if u.scheme not in ("rtsp", "rtsps") or not u.hostname:
                raise ValueError("Use an RTSP or RTSPS camera URL")
            if u.hostname.lower() in ("localhost", "metadata.google.internal"):
                raise ValueError("Use the camera's routed address")
            try:
                ip = ipaddress.ip_address(u.hostname)
            except ValueError:
                ip = None
            if ip and (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
                raise ValueError("This address cannot be a camera source")
            if u.port is not None and not 1 <= u.port <= 65535:
                raise ValueError("Invalid RTSP port")
        return self




def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return salt + ":" + hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


def check_password(password, stored):
    return hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)


class Store:
    def __init__(self, settings):
        self.settings = settings
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.recordings_dir.mkdir(parents=True, exist_ok=True)
        self.path = settings.data_dir / "bodycam.sqlite3"
        with self.connect() as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    name TEXT PRIMARY KEY, role TEXT NOT NULL, password TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY, user TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cameras (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, grp TEXT NOT NULL DEFAULT '',
                    officer TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL DEFAULT 'publish', rtsp_url TEXT NOT NULL DEFAULT '',
                    publish_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    recording_enabled INTEGER NOT NULL DEFAULT 1, last_seen REAL);
                CREATE TABLE IF NOT EXISTS stream_sessions (
                    id INTEGER PRIMARY KEY, camera_id TEXT NOT NULL, started REAL NOT NULL,
                    ended REAL, last_seen REAL NOT NULL);
            """)
            for user, role, password in (
                ("admin", "admin", settings.admin_password), ("viewer", "viewer", settings.viewer_password)):
                if password:
                    row = con.execute("SELECT password FROM users WHERE name=?", (user,)).fetchone()
                    if row is None:
                        con.execute("INSERT INTO users VALUES(?,?,?)", (user, role, password_hash(password)))
                    elif not check_password(password, row["password"]):
                        con.execute("UPDATE users SET password=? WHERE name=?", (password_hash(password), user))
                        con.execute("DELETE FROM sessions WHERE user=?", (user,))
            for i in range(1, 34):
                cid = f"bodycam{i:02}"
                con.execute("INSERT OR IGNORE INTO cameras(id,name,publish_key) VALUES(?,?,?)",
                            (cid, f"BODY CAM {i:02}", secrets.token_urlsafe(24)))
            # Close intervals at their last confirmed heartbeat after a process restart.
            con.execute("UPDATE stream_sessions SET ended=last_seen WHERE ended IS NULL")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def cameras(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM cameras ORDER BY id")]

    def camera(self, cid):
        if not CAMERA_ID.fullmatch(cid):
            raise HTTPException(404, "Camera not found")
        with self.connect() as con:
            row = con.execute("SELECT * FROM cameras WHERE id=?", (cid,)).fetchone()
        if not row:
            raise HTTPException(404, "Camera not found")
        return dict(row)

    def session(self, token_hash):
        with self.connect() as con:
            row = con.execute("""SELECT users.name,users.role,sessions.token FROM sessions
                JOIN users ON users.name=sessions.user WHERE token=? AND expires>?""",
                              (token_hash, time.time())).fetchone()
        return dict(row) if row else None


class Media:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(8, connect=1), trust_env=False,
                                       auth=("service", settings.internal_key))
        self.paths, self.applied = {}, {}
        self.pending_kicks = set()
        self.online, self.checked_at = False, 0
        self.lock = asyncio.Lock()

    async def apply(self, cam):
        path = "live/" + cam["id"]
        config = {"source": cam["rtsp_url"] if cam["mode"] == "rtsp" and cam["enabled"]
                  else "publisher", "record": bool(cam["recording_enabled"] and cam["enabled"]),
                  "sourceOnDemand": False, "rtspTransport": "tcp",
                  "srtPublishPassphrase": cam["publish_key"]}
        if not config["source"]:
            return False
        if self.applied.get(path) == config:
            return True
        encoded = quote(path, safe="/")
        r = await self.client.patch(self.settings.media_api + "/v3/config/paths/patch/" + encoded,
                                    json=config)
        if r.status_code == 404:
            r = await self.client.post(self.settings.media_api + "/v3/config/paths/add/" + encoded,
                                       json=config)
        r.raise_for_status()
        self.applied[path] = config
        return True

    async def kick(self, cid):
        self.pending_kicks.add(cid)
        response = await self.client.get(self.settings.media_api+"/v3/paths/get/live/"+cid)
        if response.status_code == 404:
            self.pending_kicks.discard(cid)
            return
        response.raise_for_status()
        source = response.json().get("source") or {}
        routes = {"rtmpConn":"rtmp/conns", "rtmpsConn":"rtmps/conns",
                  "rtspSession":"rtsp/sessions", "rtspsSession":"rtsps/sessions",
                  "srtConn":"srt/conns", "webRTCSession":"webrtc/sessions"}
        if source.get("type") in routes and source.get("id"):
            result = await self.client.post(self.settings.media_api+"/v3/"+
                routes[source["type"]]+"/kick/"+source["id"])
            if result.status_code != 404:
                result.raise_for_status()
        self.pending_kicks.discard(cid)

    async def list_items(self, route):
        items, page = [], 0
        while True:
            response = await self.client.get(self.settings.media_api+route,
                params={"itemsPerPage":100,"page":page})
            response.raise_for_status()
            payload = response.json()
            items.extend(payload.get("items", []))
            if page+1 >= payload.get("pageCount", 1):
                return items
            page += 1

    async def poll(self):
        async with self.lock:
            try:
                observation_started = time.time()
                items = await self.list_items("/v3/paths/list")
                configured = {item["name"]:item for item in await self.list_items("/v3/config/paths/list")}
                if not self.online:
                    self.applied.clear()
                else:
                    # Runtime path settings vanish on a restart, including one completed between polls.
                    self.applied = {path:values for path,values in self.applied.items()
                        if path in configured and all(configured[path].get(key)==value for key,value in values.items())}
                self.online = True
                self.checked_at = observation_started
                self.paths = {x["name"]:x for x in items}
                now = self.checked_at
                with self.store.connect() as con:
                    for cam in self.store.cameras():
                        active = bool(self.paths.get("live/" + cam["id"], {}).get("ready"))
                        previous = con.execute("SELECT id FROM stream_sessions WHERE camera_id=? AND ended IS NULL",
                                               (cam["id"],)).fetchone()
                        if active:
                            con.execute("UPDATE cameras SET last_seen=? WHERE id=?", (now, cam["id"]))
                            if previous:
                                con.execute("UPDATE stream_sessions SET last_seen=? WHERE id=?", (now, previous["id"]))
                            else:
                                con.execute("INSERT INTO stream_sessions(camera_id,started,last_seen) VALUES(?,?,?)",
                                            (cam["id"],now,now))
                        elif previous:
                            con.execute("UPDATE stream_sessions SET ended=last_seen WHERE id=?", (previous["id"],))
                for cam in self.store.cameras():
                    if cam["id"] in self.pending_kicks or (not cam["enabled"] and self.paths.get("live/"+cam["id"],{}).get("ready")):
                        await self.kick(cam["id"])
                    await self.apply(cam)
            except (httpx.HTTPError, ValueError, KeyError):
                self.online = False
                self.paths = {}

    async def run(self):
        while True:
            await self.poll()
            await asyncio.sleep(self.settings.poll_seconds)


def create_app(settings=None):
    cfg = settings or Settings()
    cfg.data_dir = Path(cfg.data_dir)
    if len(cfg.admin_password) < 12 or not cfg.internal_key:
        raise RuntimeError("Run setup.py to configure a strong administrator password and internal key")
    store = Store(cfg)
    media = Media(store, cfg)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(media.run())
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await media.client.aclose()

    app = FastAPI(title="East Godavari Body Camera Monitor", lifespan=lifespan,
                  docs_url=None, redoc_url=None)
    app.state.settings, app.state.store, app.state.media = cfg, store, media
    app.add_middleware(CORSMiddleware, allow_origins=cfg.cors_origins,
        allow_methods=["GET","POST","PATCH","DELETE","OPTIONS"],
        allow_headers=["Authorization","Content-Type","Range","If-Match"],
        expose_headers=["Location","ETag","Content-Range","Content-Disposition","Accept-Ranges"])
    failures = {}

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/") or request.url.path.startswith("/internal/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def current_user(request: Request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            raise HTTPException(401, "Please sign in")
        user = store.session(hashlib.sha256(auth[7:].encode()).hexdigest())
        if not user:
            raise HTTPException(401, "Session expired. Please sign in")
        return user

    def admin(user=Depends(current_user)):
        if user["role"] != "admin":
            raise HTTPException(403, "Administrator access required")
        return user

    def ticket(scope, user):
        payload = base64.urlsafe_b64encode(json.dumps(
            {"s":user["token"],"scope":scope,"exp":time.time()+3600},
            separators=(",",":")).encode()).decode().rstrip("=")
        sig = hmac.new(cfg.internal_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return payload + "." + sig

    def media_user(request, scope):
        if request.headers.get("authorization"):
            return current_user(request)
        token = request.query_params.get("ticket", "")
        try:
            payload, sig = token.rsplit(".",1)
            expected = hmac.new(cfg.internal_key.encode(),payload.encode(),hashlib.sha256).hexdigest()
            data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not hmac.compare_digest(sig,expected) or data["scope"] != scope or data["exp"] <= time.time():
                raise ValueError()
            user = store.session(data["s"])
            if not user:
                raise ValueError()
            return user
        except (ValueError, KeyError, TypeError):
            raise HTTPException(401, "Media access expired. Reopen the video") from None

    @app.get("/healthz")
    def health():
        return {"status":"ok"}

    @app.post("/api/auth/login")
    def auth_login(data: Login, request: Request):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = [t for t in failures.get(ip, []) if now-t < 300]
        if len(attempts) >= 20:
            raise HTTPException(429, "Too many attempts. Try again in five minutes")
        with store.connect() as con:
            row = con.execute("SELECT * FROM users WHERE name=?", (data.username,)).fetchone()
            candidate = row["password"] if row else con.execute("SELECT password FROM users WHERE name='admin'").fetchone()[0]
            valid = check_password(data.password, candidate)
            if not row or not valid:
                failures[ip] = attempts + [now]
                raise HTTPException(401, "Incorrect username or password")
            token = secrets.token_urlsafe(32)
            con.execute("DELETE FROM sessions WHERE expires<?", (now,))
            con.execute("INSERT INTO sessions VALUES(?,?,?)",
                        (hashlib.sha256(token.encode()).hexdigest(),row["name"],now+12*3600))
        failures.pop(ip, None)
        return {"token":token,"role":row["role"],"username":row["name"]}

    @app.get("/api/auth/me")
    def auth_me(user=Depends(current_user)):
        return {"username":user["name"],"role":user["role"]}

    @app.post("/api/auth/logout")
    def auth_logout(user=Depends(current_user)):
        with store.connect() as con:
            con.execute("DELETE FROM sessions WHERE token=?", (user["token"],))
        return {"ok":True}

    @app.get("/api/cameras")
    def camera_list(user=Depends(current_user)):
        now = time.time()
        with store.connect() as con:
            start = datetime.now(timezone(timedelta(hours=5,minutes=30))).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            hours = sum(max(0,min(now,r["ended"] or r["last_seen"])-max(start,r["started"]))
                for r in con.execute("SELECT * FROM stream_sessions WHERE last_seen>=?", (start,))) / 3600
        cameras = []
        for cam in store.cameras():
            state = media.paths.get("live/"+cam["id"], {})
            if not cam["enabled"]:
                status = "disabled"
            elif not media.online:
                status = "unknown"
            else:
                status = "online" if state.get("ready") else "offline"
            writing = False
            if status == "online" and cam["recording_enabled"]:
                directory = cfg.recordings_dir / "live" / cam["id"]
                latest_file = max(directory.glob("*.mp4"), key=lambda p:p.name, default=None)
                if latest_file:
                    try:
                        stat = latest_file.stat()
                        writing = stat.st_size > 0 and now-stat.st_mtime < 6
                    except OSError:
                        pass
            cameras.append({
                "id":cam["id"],"name":cam["name"],"group":cam["grp"],"officer":cam["officer"],
                "model":cam["model"],"mode":cam["mode"],"enabled":bool(cam["enabled"]),
                "recording_enabled":bool(cam["recording_enabled"]),"recording":writing,
                "source_configured":bool(cam["rtsp_url"]),"status":status,"last_seen":cam["last_seen"],
                "tracks":state.get("tracks",[]),"bytes_received":state.get("bytesReceived",0)})
        usage = shutil.disk_usage(cfg.recordings_dir)
        return {"cameras":cameras,"media_online":media.online,"checked_at":media.checked_at,
                "summary":{"registered":len(cameras),"online":sum(c["status"]=="online" for c in cameras),
                           "camera_hours_today":round(hours,3),
                           "storage_free_bytes":usage.free,"storage_total_bytes":usage.total}}

    @app.patch("/api/cameras/{cid}")
    async def camera_update(cid: str, data: CameraUpdate, user=Depends(admin)):
        cam = store.camera(cid)
        changes = data.model_dump(exclude_none=True)
        if changes.get("mode",cam["mode"]) == "rtsp" and not changes.get("rtsp_url",cam["rtsp_url"]):
            raise HTTPException(422, "Enter the RTSP URL reachable through your VPN or private APN")
        with store.connect() as con:
            for name, value in changes.items():
                column = "grp" if name == "group" else name
                con.execute(f"UPDATE cameras SET {column}=? WHERE id=?", (value,cid))
        applied = False
        try:
            if changes.get("enabled") is False or changes.get("mode",cam["mode"]) != cam["mode"]:
                await media.kick(cid)
            applied = await media.apply(store.camera(cid))
        except httpx.HTTPError:
            pass
        return {"saved":True,"applied":applied,
                "message":"Camera saved" if applied else "Saved. Waiting for the streaming server to apply settings"}

    @app.get("/api/cameras/{cid}/connection")
    def connection(cid: str, user=Depends(admin)):
        cam = store.camera(cid)
        host, password = cfg.publish_host, cam["publish_key"]
        query = urlencode({"user":cid,"pass":password})
        return {"camera_id":cid,"path":"live/"+cid,"server_host":host,"username":cid,
                "password":password,"mode":cam["mode"],
                "rtmp_url":f"rtmp://{host}:{cfg.rtmp_port}/live/{cid}?{query}",
                "rtmp_server":f"rtmp://{host}:{cfg.rtmp_port}/live",
                "rtmp_stream_key":cid+"?"+query,
                "rtsp_publish_url":f"rtsp://{cid}:{quote(password,safe='')}@{host}:{cfg.rtsp_port}/live/{cid}",
                "srt_url":f"srt://{host}:{cfg.srt_port}?streamid=publish:live/{cid}:{cid}:{password}&passphrase={password}&pbkeylen=32&pkt_size=1316",
                "note":"Publishing must be initiated by the camera over its SIM. RTSP-only cameras need a routed VPN/private APN. Configure H.264 on the camera for browser compatibility."}

    @app.post("/api/cameras/{cid}/rotate-key")
    async def rotate_key(cid: str, user=Depends(admin)):
        store.camera(cid)
        with store.connect() as con:
            con.execute("UPDATE cameras SET publish_key=? WHERE id=?", (secrets.token_urlsafe(24),cid))
        try:
            await media.kick(cid)
            await media.apply(store.camera(cid))
        except httpx.HTTPError:
            pass
        return {"ok":True,"message":"New key ready. Update this camera's publishing credentials"}

    @app.post("/api/cameras/{cid}/probe")
    def probe(cid: str, user=Depends(admin)):
        cam = store.camera(cid)
        if cam["mode"] != "rtsp" or not cam["rtsp_url"]:
            raise HTTPException(422, "Configure a reachable RTSP source first")
        try:
            r = subprocess.run(["ffprobe","-v","error","-rtsp_transport","tcp","-timeout","5000000",
                "-select_streams","v:0","-show_entries","stream=codec_name,width,height,r_frame_rate",
                "-of","json",cam["rtsp_url"]],capture_output=True,timeout=8,check=True)
            tracks = json.loads(r.stdout).get("streams",[])
            if not tracks:
                raise ValueError()
            return {"reachable":True,"video":tracks[0]}
        except (subprocess.SubprocessError, OSError, ValueError):
            return {"reachable":False,"message":"No video received. Check the VPN/APN route, camera credentials and RTSP path"}

    @app.post("/internal/media-auth")
    async def media_auth(request: Request):
        data = await request.json()
        if not isinstance(data, dict) or any(not isinstance(data.get(k, ""), str) for k in ("action", "path", "user", "password")):
            raise HTTPException(401, "Denied")
        action, path = data.get("action"), data.get("path","")
        user, password = data.get("user",""), data.get("password","")
        if action in ("api","read","playback") and user == "service" and hmac.compare_digest(password,cfg.internal_key):
            return {"ok":True}
        if action == "publish" and path.startswith("live/"):
            try:
                cam = store.camera(path[5:])
            except HTTPException:
                raise HTTPException(401, "Denied") from None
            if cam["enabled"] and cam["mode"] == "publish" and user == cam["id"] and hmac.compare_digest(password,cam["publish_key"]):
                return {"ok":True}
        raise HTTPException(401, "Denied")



    def recording_files():
        root = cfg.recordings_dir.resolve()
        items = []
        cams = {cam["id"]:cam for cam in store.cameras()}
        for cid, cam in cams.items():
            directory = root / "live" / cid
            camera_items = []
            for path in directory.glob("*.mp4"):
                if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                    continue
                stat = path.stat()
                rel = path.relative_to(root).as_posix()
                try:
                    started = datetime.strptime(path.stem,"%Y-%m-%d_%H-%M-%S-%f").replace(tzinfo=timezone.utc).timestamp()
                except ValueError:
                    started = None
                camera_items.append({"id":hashlib.sha256(rel.encode()).hexdigest()[:24],
                    "camera_id":cid,"camera_name":cam["name"],"group":cam["grp"],"filename":path.name,
                    "started_at":started,"size_bytes":stat.st_size,"modified_at":stat.st_mtime,
                    "in_progress":False,
                    "_path":path})
            active = bool(media.paths.get("live/"+cid,{}).get("ready"))
            may_record = active and media.applied.get("live/"+cid,{}).get("record",True)
            if camera_items:
                newest = max(camera_items,key=lambda x:x["started_at"] or x["modified_at"])
                # Stalls and writes after the last status snapshot cannot establish segment completion.
                newest["in_progress"] = not media.online or may_record or newest["modified_at"]>media.checked_at
            items.extend(camera_items)
        return sorted(items,key=lambda x:x["started_at"] or x["modified_at"],reverse=True)

    def recording(rec_id):
        for item in recording_files():
            if item["id"] == rec_id:
                return item
        raise HTTPException(404, "Recording not found")

    @app.get("/api/recordings")
    def recordings(camera: str = "", q: str = "", offset: int = 0, limit: int = 100,
                   user=Depends(current_user)):
        if offset < 0 or not 1 <= limit <= 500:
            raise HTTPException(422, "Invalid page")
        files = recording_files()
        selected = [f for f in files if (not camera or f["camera_id"]==camera) and
                    q.lower() in (f["camera_name"]+" "+f["group"]+" "+f["filename"]).lower()]
        return {"items":[{k:v for k,v in f.items() if k!="_path"} for f in selected[offset:offset+limit]],
                "total":len(selected),"total_size_bytes":sum(f["size_bytes"] for f in files)}

    @app.post("/api/recordings/{rec_id}/ticket")
    def recording_ticket(rec_id: str, user=Depends(current_user)):
        recording(rec_id)
        return {"url":f"/api/recordings/{rec_id}/content?ticket="+ticket("record:"+rec_id,user)}

    @app.get("/api/recordings/{rec_id}/metadata")
    def recording_metadata(rec_id: str, user=Depends(current_user)):
        item = recording(rec_id)
        try:
            r = subprocess.run(["ffprobe","-v","error","-show_entries",
                "format=duration:stream=codec_name,codec_type,width,height","-of","json",str(item["_path"])],
                capture_output=True,timeout=10,check=True)
            return json.loads(r.stdout)
        except (subprocess.SubprocessError, OSError, ValueError):
            raise HTTPException(409, "Recording is still being written or cannot be decoded") from None

    @app.get("/api/recordings/{rec_id}/content")
    def recording_content(rec_id: str, request: Request, download: bool = False):
        media_user(request,"record:"+rec_id)
        item = recording(rec_id)
        if item["in_progress"] and download:
            raise HTTPException(409, "Recording completion is pending. Download a completed segment")
        return FileResponse(item["_path"],media_type="video/mp4",filename=item["filename"],
                            content_disposition_type="attachment" if download else "inline")

    @app.post("/api/streams/{cid}/ticket")
    def stream_ticket(cid: str, user=Depends(current_user)):
        store.camera(cid)
        return {"ticket":ticket("stream:"+cid,user)}

    async def proxy(request, cid, protocol, resource):
        cam = store.camera(cid)
        media_user(request,"stream:"+cid)
        if not cam["enabled"]:
            raise HTTPException(409, "Camera disabled")
        if ".." in resource.split("/") or not re.fullmatch(r"[A-Za-z0-9._/-]*",resource):
            raise HTTPException(400, "Invalid media path")
        suffix = "/"+resource if resource else ""
        base = cfg.media_hls if protocol == "hls" else cfg.media_webrtc
        endpoint = f"{base}/live/{cid}" + ("/whep" if protocol=="whep" else "") + suffix
        headers = {key:value for key,value in request.headers.items()
                   if key.lower() in ("content-type","if-match","range")}
        # MediaMTX's CDN mode is designed for an authenticated proxy and avoids browser session cookies.
        if protocol == "hls":
            headers["Authorization"] = "Bearer "+cfg.internal_key
        params = {key:value for key,value in request.query_params.items() if key.startswith("_HLS_")}
        try:
            upstream = await media.client.send(media.client.build_request(
                request.method,endpoint,headers=headers,params=params,
                content=await request.body()),stream=True,
                auth=None if protocol == "hls" else httpx.USE_CLIENT_DEFAULT)
        except httpx.HTTPError:
            raise HTTPException(503, "Streaming server is unavailable") from None
        response_headers = {key:value for key,value in upstream.headers.items() if key.lower() in (
            "content-type","cache-control","etag","accept-ranges","content-range")}
        if protocol == "whep":
            body = await upstream.aread()
            await upstream.aclose()
            location = upstream.headers.get("location")
            if location:
                session_path = urlparse(location).path.split("/whep",1)[-1]
                if re.fullmatch(r"/[A-Za-z0-9-]+",session_path):
                    response_headers["Location"] = f"/api/streams/{cid}/whep"+session_path
            return Response(body,status_code=upstream.status_code,headers=response_headers)
        if resource.endswith(".m3u8") and upstream.status_code == 200:
            body = (await upstream.aread()).decode()
            await upstream.aclose()
            media_ticket = request.query_params.get("ticket")
            if media_ticket:
                def add_ticket(uri):
                    return uri + ("&" if "?" in uri else "?") + "ticket=" + quote(media_ticket,safe="")
                lines = []
                for line in body.splitlines():
                    if line and not line.startswith("#"):
                        line = add_ticket(line)
                    elif 'URI="' in line:
                        line = re.sub(r'URI="([^"]+)"',lambda m:'URI="'+add_ticket(m[1])+'"',line)
                    lines.append(line)
                body = "\n".join(lines)+"\n"
            return Response(body,media_type="application/vnd.apple.mpegurl",headers={"Cache-Control":"no-store"})
        async def chunks():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()
        return StreamingResponse(chunks(),status_code=upstream.status_code,headers=response_headers)

    @app.get("/api/streams/{cid}/hls/{resource:path}")
    async def hls_proxy(cid: str, resource: str, request: Request):
        return await proxy(request,cid,"hls",resource)

    @app.api_route("/api/streams/{cid}/whep",methods=["POST","OPTIONS"])
    @app.api_route("/api/streams/{cid}/whep/{resource:path}",methods=["PATCH","DELETE","OPTIONS"])
    async def whep_proxy(cid: str, request: Request, resource: str = ""):
        return await proxy(request,cid,"whep",resource)

    if cfg.frontend_dir.exists():
        app.mount("/",StaticFiles(directory=cfg.frontend_dir,html=True),name="dashboard")
    return app
