"""A fast MediaMTX restart must not restore recording for an opted-out camera."""
import asyncio

import httpx

from backend.app import Media, Settings, Store


def test_quick_media_restart_restores_persisted_recording_preference(tmp_path):
    async def scenario():
        settings=Settings(data_dir=tmp_path,admin_password="Recovery-Admin-123!",
            internal_key="recovery-service-key")
        store=Store(settings)
        with store.connect() as con:
            con.execute("UPDATE cameras SET recording_enabled=0 WHERE id='bodycam01'")
        media=Media(store,settings)
        await media.client.aclose()
        restored={}
        def server(request):
            path=request.url.path
            if path in ("/v3/paths/list","/v3/config/paths/list"):
                return httpx.Response(200,json={"items":[],"pageCount":1})
            if request.method=="PATCH":
                return httpx.Response(404,json={})
            if request.method=="POST" and "/add/" in path:
                import json
                restored[path.split("/add/",1)[1]]=json.loads(request.content)
                return httpx.Response(200,json={})
            return httpx.Response(404,json={})
        media.client=httpx.AsyncClient(transport=httpx.MockTransport(server),trust_env=False)
        media.online=True  # The restart completed between polls; no failed request was observed.
        cam=store.camera("bodycam01")
        media.applied["live/bodycam01"]={"source":"publisher","record":False,
            "sourceOnDemand":False,"rtspTransport":"tcp","srtPublishPassphrase":cam["publish_key"]}
        try:
            await media.poll()
            assert restored["live/bodycam01"]["record"] is False
            assert media.online
        finally:
            await media.client.aclose()
    asyncio.run(scenario())
