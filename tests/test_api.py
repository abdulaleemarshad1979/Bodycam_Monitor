import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def server(tmp_path):
    from backend.app import Settings, create_app
    settings = Settings(data_dir=tmp_path, admin_password="Correct-Admin-123!",
                        viewer_password="Correct-Viewer-123!", internal_key="test-service-secret",
                        publish_host="cams.example.test", poll_seconds=600)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, app


def login(client, viewer=False):
    response = client.post('/api/auth/login', json={
        'username': 'viewer' if viewer else 'admin',
        'password': 'Correct-Viewer-123!' if viewer else 'Correct-Admin-123!'})
    assert response.status_code == 200
    return {'Authorization': 'Bearer ' + response.json()['token']}


def test_camera_data_requires_login(server):
    client, _ = server
    assert client.get('/api/cameras').status_code == 401
    assert client.post('/api/auth/login', json={'username':'admin','password':'incorrect'}).status_code == 401


def test_33_camera_inventory_contains_no_fabricated_media_or_secrets(server):
    client, _ = server
    headers = login(client)
    payload = client.get('/api/cameras', headers=headers).json()
    assert len(payload['cameras']) == 33
    assert all(c['status'] != 'online' for c in payload['cameras'])
    assert all('publish_key' not in c and 'rtsp_url' not in c for c in payload['cameras'])
    recordings = client.get('/api/recordings', headers=headers).json()
    assert recordings['items'] == []
    assert payload['summary']['camera_hours_today'] == 0


def test_viewer_cannot_change_settings_or_see_sim_credentials(server):
    client, _ = server
    headers = login(client, viewer=True)
    assert client.patch('/api/cameras/bodycam01', headers=headers, json={'name':'Patrol 1'}).status_code == 403
    assert client.get('/api/cameras/bodycam01/connection', headers=headers).status_code == 403


def test_sim_publish_urls_have_unique_paths_and_camera_bound_keys(server):
    client, _ = server
    headers = login(client)
    a = client.get('/api/cameras/bodycam01/connection', headers=headers).json()
    b = client.get('/api/cameras/bodycam02/connection', headers=headers).json()
    assert 'cams.example.test:11935/live/bodycam01' in a['rtmp_url']
    assert 'streamid=publish:live/bodycam01:' in a['srt_url']
    assert a['password'] != b['password']
    message = {'action':'publish','protocol':'rtmp','path':'live/bodycam01',
               'user':'bodycam01','password':a['password']}
    assert client.post('/internal/media-auth',json=message).status_code == 200
    message['path']='live/bodycam02'
    assert client.post('/internal/media-auth',json=message).status_code == 401


def test_disabling_camera_revokes_publishing(server):
    client, _ = server
    headers = login(client)
    key = client.get('/api/cameras/bodycam01/connection',headers=headers).json()['password']
    client.patch('/api/cameras/bodycam01',headers=headers,json={'enabled':False})
    assert client.post('/internal/media-auth',json={'action':'publish','path':'live/bodycam01',
        'user':'bodycam01','password':key}).status_code == 401


def test_key_rotation_invalidates_previous_publish_credentials(server):
    client, _ = server
    headers = login(client)
    old = client.get('/api/cameras/bodycam01/connection',headers=headers).json()['password']
    assert client.post('/api/cameras/bodycam01/rotate-key',headers=headers).status_code == 200
    new = client.get('/api/cameras/bodycam01/connection',headers=headers).json()['password']
    assert old != new
    assert client.post('/internal/media-auth',json={'action':'publish','path':'live/bodycam01',
        'user':'bodycam01','password':old}).status_code == 401


def test_rtsp_settings_validate_protocol_and_hide_source_password(server):
    client, _ = server
    headers = login(client)
    assert client.patch('/api/cameras/bodycam01',headers=headers,json={
        'mode':'rtsp','rtsp_url':'http://169.254.169.254/latest/meta-data'}).status_code == 422
    r = client.patch('/api/cameras/bodycam01',headers=headers,json={
        'mode':'rtsp','rtsp_url':'rtsp://operator:hidden-camera-password@10.55.1.5:554/main',
        'name':'Patrol 1','group':'Rjy'})
    assert r.status_code == 200
    public = client.get('/api/cameras',headers=headers).text
    assert 'hidden-camera-password' not in public
    assert 'Patrol 1' in public


def test_recordings_are_real_files_with_authenticated_byte_range_playback(server):
    client, app = server
    headers = login(client)
    directory = app.state.settings.recordings_dir / 'live' / 'bodycam01'
    directory.mkdir(parents=True)
    path = directory/'2026-10-10_09-00-00-000000.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi',
        '-i','testsrc2=size=160x90:rate=10','-t','1','-c:v','libx264','-y',str(path)],check=True)
    items = client.get('/api/recordings',headers=headers).json()['items']
    assert len(items) == 1 and items[0]['size_bytes'] == path.stat().st_size
    ticket = client.post('/api/recordings/'+items[0]['id']+'/ticket',headers=headers).json()['url']
    response=client.get(ticket,headers={'Range':'bytes=0-31'})
    assert response.status_code == 206 and response.content == path.read_bytes()[:32]
    assert client.get('/api/recordings/'+items[0]['id']+'/content').status_code == 401
    assert client.get('/api/recordings/../../etc/passwd/content',headers=headers).status_code != 200
    client.post('/api/auth/logout',headers=headers)
    assert client.get(ticket).status_code == 401


def test_recording_symlink_cannot_expose_other_files(server,tmp_path):
    client,app=server
    directory=app.state.settings.recordings_dir/'live'/'bodycam01'; directory.mkdir(parents=True)
    outside=tmp_path/'private.txt'; outside.write_text('PRIVATE')
    (directory/'2026-10-10_09-00-00-000000.mp4').symlink_to(outside)
    assert client.get('/api/recordings',headers=login(client)).json()['items'] == []




def test_logout_revokes_api_access(server):
    client,_=server
    headers=login(client)
    assert client.post('/api/auth/logout',headers=headers).status_code == 200
    assert client.get('/api/cameras',headers=headers).status_code == 401


def test_sim_stall_or_control_outage_does_not_allow_partial_download(server):
    import time
    client,app=server
    headers=login(client)
    directory=app.state.settings.recordings_dir/'live'/'bodycam01';directory.mkdir(parents=True)
    old=directory/'2026-10-10_08-00-00-000000.mp4';old.write_bytes(b'completed segment')
    latest=directory/'2026-10-10_09-00-00-000000.mp4';latest.write_bytes(b'open segment')
    os.utime(old,(time.time()-100,time.time()-100))
    os.utime(latest,(time.time()-11,time.time()-11))
    media=app.state.media;media.online=True;media.paths={'live/bodycam01':{'ready':True}}
    items=client.get('/api/recordings',headers=headers).json()['items']
    assert items[0]['in_progress'] is True and items[1]['in_progress'] is False
    url=client.post('/api/recordings/'+items[0]['id']+'/ticket',headers=headers).json()['url']
    assert client.get(url+'&download=true').status_code==409
    media.online=False;media.paths={}
    assert client.get(url+'&download=true').status_code==409
    media.online=True
    media.checked_at=time.time()-20  # A publisher may have started since the last inactive snapshot.
    assert client.get(url+'&download=true').status_code==409
    media.checked_at=time.time()
    assert client.get(url+'&download=true').status_code==200
