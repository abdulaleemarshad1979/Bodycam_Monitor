# East Godavari Body Camera Monitoring System

Frontend and backend for 33 SIM-equipped body-worn cameras, using the supplied drone-dashboard layout.

## Included

- Police header and responsive three-column live-camera grid.
- All Streams, Active Feeds, Pushkaralu/Rjy group filters, search and full-screen viewing.
- Separate admin and viewer accounts with revocable sessions.
- Per-camera names, group/station, officer, model and publishing credentials.
- RTMP / encrypted SRT / RTSP publishing over each camera's SIM internet connection.
- RTSP pull mode when a working VPN/private-APN route reaches the camera.
- WebRTC viewing, HLS fallback and stalled-stream recovery.
- Original-quality continuous recording, approximately 50-minute segments, real archive listing, authenticated playback and downloads.
- Optional bounded person detector, visible-person threshold alerts and a CSV of actual analysis results.

There are no sample videos, invented recording entries, manufactured AI counts or fake online cameras.

## Deploy on your Linux server

Prerequisites: Docker Engine, Docker Compose v2, Python 3 and unzip.
Dependencies for the backend and video tools are installed inside the containers.

Place the ZIP on the server, then run this block in its directory:

~~~bash
unzip Bodycam_Monitor_Deployment.zip
cd Bodycam_Monitor
python3 setup.py
docker compose up -d --build
~~~

Setup asks for the camera-reachable publishing hostname/IP, the HTTPS API address and the two account passwords. It preserves existing settings and recordings on reruns.

Default ports deliberately differ from the drone system:

| Service | Host port | Access |
|---|---|---|
| Dashboard/API | 127.0.0.1:18081 TCP | Through your existing HTTPS reverse proxy |
| RTMP ingest | 11935 TCP | Camera publishing |
| RTSP ingest | 18554 TCP | Camera publishing, preferably over a private tunnel |
| SRT ingest | 18890 UDP | Encrypted camera publishing |
| WebRTC media | 18189 UDP | Website viewers |
| MediaMTX control / HLS / WHEP | Docker network only | Not published on host |

The same external/internal WebRTC port is configured so ICE advertises the correct port. Changing WEBRTC_PORT in .env changes both.

Add a separate virtual host using deploy/nginx-bodycam.conf. Replace BODYCAM_HOST, enable HTTPS using your normal certificate workflow, and leave the existing drone virtual host in place. Port 18081 is loopback-bound by default.

The standalone website can then connect to that HTTPS backend. CORS includes the published dashboard origin and the API origin entered during setup. If you use another frontend address, add its exact origin to CORS_ORIGINS in .env and restart the app.

Public camera ingress must be reachable from the mobile network. A normal HTTP proxy or HTTP tunnel does not automatically carry RTMP, RTSP, SRT or WebRTC UDP. Use a direct media hostname/IP and the corresponding forwarded ports, or a correctly routed private APN/VPN. The API hostname may be different from the media hostname.

## Connect a SIM camera

1. Put an active data SIM in the camera and enable its mobile-data/APN connection.
2. Sign in as admin and open Camera setup.
3. Select that camera and enter its actual name, station/group, officer and model.
4. Keep "SIM: camera publishes to central server" selected if the firmware can publish.
5. Enter the displayed custom server / stream key / username / password on the camera.
6. For SRT, use caller mode and the displayed URL, including its encryption passphrase.
7. Configure H.264 video and automatic reconnect on the camera. Start its live-publishing function.
8. Once the feed reaches MediaMTX, the dashboard reports it online and recording starts when enabled.

A camera exposing an RTSP server is not necessarily able to publish RTSP. If its only feature is RTSP reading and its SIM is behind CGNAT, website code cannot make that local RTSP address remotely reachable.

For an RTSP-only camera, provision a supported camera-side VPN or a carrier-routed private APN with a route from the central server. Select RTSP mode and enter the actual reachable source URL. Save, then run "Test saved RTSP source".

Installing a VPN only on the central server does not establish a route to an otherwise unreachable camera.

The exact camera brand/model and firmware options still need to be checked for physical onboarding.

## Recordings and storage

MediaMTX records the incoming source stream without re-encoding. A 50-minute setting is a minimum segment length; keyframes can move the boundary slightly. Disconnects or reconnections close the current segment and start another.

Completed MP4 files appear in Recordings with their real camera ID, timestamp and file size. Video duration is obtained from the file/player, never from sample metadata. Downloads of an actively written segment are blocked.

The newest segment stays protected during a SIM stall and while the media control API is unavailable. Older completed segments remain downloadable. Completion becomes known when the control API confirms that recording has stopped or a newer segment has begun.

The camera card says "Recording" only when the server sees a non-empty recent recording file for that online camera.

Recordings live under data/recordings/live/bodycamNN/.
Database, account configuration and stream credentials live under data/. The setup tool protects this directory and its secret files.

Automatic deletion is disabled. Watch available storage: 33 simultaneous feeds at 2 Mbps consume approximately 29.7 GB/hour, or 712.8 GB/day before overhead. A full disk prevents further video recording. Choose retention/archiving according to your actual capacity.

Pause viewer playback or switch grid filters without affecting server recording.

## Accounts and updates

Setup creates usernames admin and viewer using the passwords you enter. There are no shipped default passwords.

To change passwords, edit data/config.json locally and recreate the app container:

~~~bash
docker compose up -d --force-recreate app
~~~

Changing a role's password revokes its previous sessions at startup. Keep INTERNAL_KEY unchanged unless you are deliberately rotating service credentials and updating data/mediamtx.yml and recreating affected containers.

HTTP authorization is rechecked for HLS, recordings and WebRTC signalling. Logout closes the dashboard's WebRTC receiver. An established custom WebRTC receiver is authorized at negotiation time and can continue until that media session closes; immediate transport revocation requires terminating that session on the media server.

Generate a new per-camera publishing key from Camera setup to revoke the old key and disconnect its existing publisher when the media control API is reachable.

Operational checks:

~~~bash
docker compose ps
docker compose logs --tail=80 app mediamtx
~~~

Do not share camera publishing URLs or the data/config.json file.

The source includes a reusable static frontend. The deployed ChatGPT Site is private; the server package hosts its own dashboard for admin/viewer users.

## Verification

The test suite checks authentication, viewer restrictions, 33-camera initialization, camera-bound SIM credentials, key rotation, source validation, analytics ingestion, real-file recording access and session/ticket revocation.

Run:

~~~bash
python3 -m pip install -r backend/requirements.txt pytest httpx2 PyYAML
python3 -m pytest
node --check dist/app.js
~~~

For the real streaming test, use a MediaMTX v1.21.2 binary:

~~~bash
MEDIAMTX_BIN=/path/to/mediamtx python3 -m pytest tests/test_stream_integration.py
~~~

It starts the actual backend and MediaMTX, publishes a generated FFmpeg test stream, checks authenticated HLS, reads the resulting MP4 metadata, downloads the actual video bytes and verifies that disabling a camera disconnects its publisher. Only the test changes segment duration to two seconds.

Physical SIM cameras, carrier/APN routing and the optional ML model still require verification on your hardware. Browser visual QA and Docker execution were unavailable in the build environment. Native HLS streaming was verified; completing WebRTC negotiation was blocked by this environment's network-interface restrictions, so the real WHEP check emits a warning here and must be repeated on the Linux server.
