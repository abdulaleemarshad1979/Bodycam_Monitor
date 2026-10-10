#!/usr/bin/env python3
"""Prepare an isolated body-camera deployment without changing the drone system."""
import getpass
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
CONFIG = ROOT / "data" / "config.json"


def media_config(settings):
    target = ROOT / "data" / "mediamtx.yml"
    if not target.exists():
        fd = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as out:
            out.write((ROOT/"deploy/mediamtx.yml").read_text())
            out.write("\nhlsCDNSecret: "+json.dumps(settings["INTERNAL_KEY"])+"\n")


def password(label):
    while True:
        first = getpass.getpass(label + " (at least 12 characters): ")
        if len(first) < 12 or any(ord(c) < 32 for c in first):
            print("Use at least 12 characters without control characters.")
            continue
        if first == getpass.getpass("Confirm " + label.lower() + ": "):
            return first
        print("Passwords did not match.")


def check_ports():
    checks = [(18081,socket.SOCK_STREAM),(11935,socket.SOCK_STREAM),
              (18554,socket.SOCK_STREAM),(18890,socket.SOCK_DGRAM),(18189,socket.SOCK_DGRAM)]
    busy = []
    for port, kind in checks:
        with socket.socket(socket.AF_INET,kind) as sock:
            try:
                sock.bind(("0.0.0.0",port))
            except OSError:
                busy.append(port)
    if busy:
        print("These default ports are in use:", ", ".join(map(str,busy)))
        print("If this deployment is already running, leave it running. Otherwise edit .env to choose free ports.")
    return busy


def main():
    os.chdir(ROOT)
    if os.name != "posix":
        sys.exit("Use this deployment package on a Linux server with Docker Compose.")
    if not shutil.which("docker"):
        sys.exit("Install Docker Engine and Docker Compose, then rerun this command.")
    result = subprocess.run(["docker","compose","version"],capture_output=True,text=True)
    if result.returncode:
        sys.exit("Docker Compose v2 is required.")
    for name in ("data","data/recordings","models"):
        (ROOT/name).mkdir(parents=True,exist_ok=True)
    (ROOT/"data").chmod(0o700)
    if ENV.exists() and CONFIG.exists():
        media_config(json.loads(CONFIG.read_text()))
        print("Existing settings and recordings preserved.")
        check_ports()
        return
    if ENV.exists() or CONFIG.exists():
        sys.exit("Partial setup detected. Preserve the existing file and complete the missing configuration manually; see README.md.")
    host = input("Hostname/IP reachable from the cameras over SIM: ").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}",host):
        sys.exit("Enter a hostname or IPv4 address without a URL prefix, port or path.")
    public = input("HTTPS dashboard/API URL [https://"+host+"]: ").strip() or "https://"+host
    u = urlparse(public)
    if u.scheme not in ("https","http") or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ("","/"):
        sys.exit("Enter a valid HTTP(S) origin without credentials or a path.")
    admin = password("Admin password")
    viewer = password("Viewer password")
    import secrets
    settings = {"ADMIN_PASSWORD":admin,"VIEWER_PASSWORD":viewer,"INTERNAL_KEY":secrets.token_urlsafe(48)}
    fd = os.open(CONFIG,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as out:
        json.dump(settings,out,indent=2)
        out.write("\n")
    media_config(settings)
    origin = "https://east-godavari-bodycam-monitor.fresh-bun-7841.chatgpt.site"
    lines = {
        "PUBLISH_HOST":host,"PUBLIC_API_URL":public.rstrip("/"),
        "CORS_ORIGINS":origin+","+public.rstrip("/"),
        "APP_BIND":"127.0.0.1","APP_PORT":"18081","RTMP_PORT":"11935","RTSP_PORT":"18554",
        "SRT_PORT":"18890","WEBRTC_PORT":"18189","BODYCAM_UID":str(os.getuid()),"BODYCAM_GID":str(os.getgid())
    }
    fd = os.open(ENV,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as out:
        out.write("\n".join(key+"="+value for key,value in lines.items())+"\n")
    busy = check_ports()
    print("\nConfigured 33 cameras. The usernames are admin and viewer.")
    print("Next: docker compose up -d --build")
    print("Local server: http://127.0.0.1:18081")
    print("Publish HTTPS through your reverse proxy using deploy/nginx-bodycam.conf.")
    print("Camera ingest ports: RTMP 11935/TCP, RTSP 18554/TCP, SRT 18890/UDP, WebRTC 18189/UDP.")
    print("Only forward the required ingest ports. The MediaMTX control API stays private.")
    print("Open Camera setup in the dashboard to get the correct credentials for each camera.")
    if busy:
        print("Resolve port conflicts in .env before starting the new deployment.")
    print("Recordings are never automatically deleted. Monitor available disk space.")


if __name__ == "__main__":
    main()
