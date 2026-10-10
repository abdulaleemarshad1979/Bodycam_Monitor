"""Real MediaMTX + FFmpeg integration. Set MEDIAMTX_BIN to run this test."""
import json
import os
import socket
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def free_ports(count):
    sockets = []
    for _ in range(count):
        s=socket.socket();s.bind(("127.0.0.1",0));sockets.append(s)
    ports=[s.getsockname()[1] for s in sockets]
    for s in sockets:s.close()
    return ports


def wait_for(check,timeout=15):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            result=check()
            if result:return result
        except (httpx.HTTPError,KeyError,ValueError):
            pass
        time.sleep(.2)
    raise AssertionError("Timed out waiting for a real stream condition")


@pytest.mark.skipif(not os.getenv("MEDIAMTX_BIN"),reason="Set MEDIAMTX_BIN for the real streaming test")
def test_sim_publish_to_hls_recording_and_download(tmp_path):
    app_port,api_port,rtmp_port,rtsp_port,hls_port,whep_port,ice_port=free_ports(7)
    data=tmp_path/"data";data.mkdir()
    key="integration-service-key-123456789"
    env=dict(os.environ,DATA_DIR=str(data),ADMIN_PASSWORD="Integration-Admin-123!",
        VIEWER_PASSWORD="Integration-Viewer-123!",INTERNAL_KEY=key,PUBLISH_HOST="127.0.0.1",
        MEDIA_API=f"http://127.0.0.1:{api_port}",MEDIA_HLS=f"http://127.0.0.1:{hls_port}",
        MEDIA_WEBRTC=f"http://127.0.0.1:{whep_port}",RTMP_PORT=str(rtmp_port),RTSP_PORT=str(rtsp_port))
    config=yaml.safe_load((ROOT/"deploy/mediamtx.yml").read_text())
    config.update(apiAddress=f"127.0.0.1:{api_port}",rtmpAddress=f"127.0.0.1:{rtmp_port}",
        rtspAddress=f"127.0.0.1:{rtsp_port}",hlsAddress=f"127.0.0.1:{hls_port}",
        webrtcAddress=f"127.0.0.1:{whep_port}",webrtcLocalUDPAddress=f"127.0.0.1:{ice_port}",
        webrtcAdditionalHosts=["127.0.0.1"],webrtcIPsFromInterfaces=False,srt=False,
        hlsCDNSecret=key,
        authHTTPAddress=f"http://127.0.0.1:{app_port}/internal/media-auth")
    config["pathDefaults"].update(recordPath=str(data/"recordings/%path/%Y-%m-%d_%H-%M-%S-%f"),
        recordSegmentDuration="2s",recordPartDuration="200ms")
    conf=tmp_path/"mediamtx.yml";conf.write_text(yaml.safe_dump(config))
    logs=(tmp_path/"services.log").open("w")
    app=subprocess.Popen([sys.executable,"-m","uvicorn","backend.app:create_app","--factory",
        "--host","127.0.0.1","--port",str(app_port),"--no-access-log"],cwd=ROOT,env=env,stdout=logs,stderr=logs)
    mtx=None;publisher=None
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{app_port}",trust_env=False,timeout=10) as client:
            wait_for(lambda:client.get("/healthz").status_code==200)
            mtx=subprocess.Popen([os.environ["MEDIAMTX_BIN"],str(conf)],cwd=tmp_path,stdout=logs,stderr=logs)
            auth=client.post("/api/auth/login",json={"username":"admin","password":"Integration-Admin-123!"})
            assert auth.status_code==200
            client.headers["Authorization"]="Bearer "+auth.json()["token"]
            wait_for(lambda:client.get("/api/cameras").json()["media_online"])
            camera=client.get("/api/cameras/bodycam01/connection").json()
            publisher=subprocess.Popen(["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-re",
                "-f","lavfi","-i","testsrc2=size=320x180:rate=10","-t","12","-c:v","libx264",
                "-profile:v","baseline","-bf","0","-g","10","-pix_fmt","yuv420p","-f","flv",camera["rtmp_url"]],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            wait_for(lambda:client.get("/api/cameras").json()["cameras"][0]["status"]=="online")
            fleet=client.get("/api/cameras").json()
            assert fleet["summary"]["online"]==1
            wait_for(lambda:client.get("/api/cameras").json()["cameras"][0]["recording"])
            # Check the real WHEP signalling proxy and session cleanup. Browser media decoding is separate.
            offer="\r\n".join([
                "v=0","o=- 1 1 IN IP4 127.0.0.1","s=-","t=0 0","a=group:BUNDLE 0",
                "m=video 9 UDP/TLS/RTP/SAVPF 96","c=IN IP4 0.0.0.0","a=mid:0",
                "a=ice-ufrag:bodycamtest","a=ice-pwd:bodycamtestingsecret123456789",
                "a=fingerprint:sha-256 "+":".join(["AA"]*32),"a=setup:actpass",
                "a=recvonly","a=rtcp-mux","a=rtpmap:96 H264/90000",
                "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",""])
            signal=client.post("/api/streams/bodycam01/whep",content=offer,
                headers={"Content-Type":"application/sdp"})
            if signal.status_code==400 and "netlinkrib: operation not permitted" in signal.text:
                warnings.warn("WHEP transport verification unavailable: this environment blocks native network-interface enumeration")
            else:
                assert signal.status_code==201,signal.text
                assert "a=ice-ufrag:" in signal.text
                session=signal.headers["location"]
                assert session.startswith("/api/streams/bodycam01/whep/")
                assert client.delete(session).status_code==200
            last_hls={}
            def get_manifest():
                response=client.get("/api/streams/bodycam01/hls/index.m3u8")
                last_hls.update(status=response.status_code,body=response.text[:180])
                if response.status_code==302 and "redirect" not in last_hls:
                    with httpx.Client(trust_env=False,auth=("service",key)) as direct:
                        upstream=direct.get(f"http://127.0.0.1:{hls_port}/live/bodycam01/index.m3u8")
                        last_hls["redirect"]=upstream.headers.get("location")
                return response if response.status_code==200 else None
            try:
                manifest=wait_for(get_manifest)
            except AssertionError:
                raise AssertionError("HLS did not become ready: "+str(last_hls)) from None
            assert "#EXTM3U" in manifest.text
            ticket=client.post("/api/streams/bodycam01/ticket").json()["ticket"]
            anonymous=httpx.Client(base_url=client.base_url,trust_env=False,timeout=10)
            assert anonymous.get("/api/streams/bodycam01/hls/index.m3u8").status_code==401
            signed=anonymous.get("/api/streams/bodycam01/hls/index.m3u8",params={"ticket":ticket})
            assert signed.status_code==200 and "ticket=" in signed.text
            for uri in signed.text.splitlines():
                if uri and not uri.startswith("#"):
                    assert anonymous.get("/api/streams/bodycam01/hls/"+uri).status_code==200
                    break
            anonymous.close()
            publisher.wait(timeout=20);assert publisher.returncode==0
            wait_for(lambda:client.get("/api/cameras").json()["cameras"][0]["status"]=="offline")
            recordings=client.get("/api/recordings").json()["items"]
            assert len(recordings)>=2
            complete=[r for r in recordings if not r["in_progress"]]
            metadata=client.get("/api/recordings/"+complete[0]["id"]+"/metadata")
            assert metadata.status_code==200
            assert float(metadata.json()["format"]["duration"])>0
            assert metadata.json()["streams"][0]["codec_name"]=="h264"
            download=client.post("/api/recordings/"+complete[0]["id"]+"/ticket").json()["url"]
            response=client.get(download+"&download=true")
            assert response.status_code==200 and len(response.content)>1000
            assert "attachment" in response.headers["content-disposition"]
            assert client.get("/api/cameras").json()["summary"]["camera_hours_today"]>0
            publisher=subprocess.Popen(["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-re",
                "-f","lavfi","-i","testsrc2=size=320x180:rate=10","-t","60","-c:v","libx264",
                "-profile:v","baseline","-bf","0","-g","10","-pix_fmt","yuv420p","-f","flv",camera["rtmp_url"]],
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            wait_for(lambda:client.get("/api/cameras").json()["cameras"][0]["status"]=="online")
            assert client.patch("/api/cameras/bodycam01",json={"enabled":False}).status_code==200
            with httpx.Client(trust_env=False,auth=("service",key)) as control:
                wait_for(lambda:not control.get(f"http://127.0.0.1:{api_port}/v3/paths/get/live/bodycam01").json().get("ready"))
            assert client.get("/api/cameras").json()["cameras"][0]["status"]=="disabled"
    finally:
        for process in (publisher,mtx,app):
            if process and process.poll() is None:
                process.terminate()
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        logs.close()
