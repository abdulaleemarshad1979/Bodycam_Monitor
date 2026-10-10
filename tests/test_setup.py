"""Installer preserves recordings/settings and safely stores special-character passwords."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import yaml


def test_setup_writes_private_secrets_and_is_safe_to_rerun(tmp_path,monkeypatch,capsys):
    root=Path(__file__).resolve().parents[1]
    spec=importlib.util.spec_from_file_location("bodycam_setup",root/"setup.py")
    setup=importlib.util.module_from_spec(spec);spec.loader.exec_module(setup)
    (tmp_path/"deploy").mkdir()
    (tmp_path/"deploy/mediamtx.yml").write_text((root/"deploy/mediamtx.yml").read_text())
    monkeypatch.setattr(setup,"ROOT",tmp_path)
    monkeypatch.setattr(setup,"ENV",tmp_path/".env")
    monkeypatch.setattr(setup,"CONFIG",tmp_path/"data/config.json")
    monkeypatch.setattr(setup.shutil,"which",lambda name:"/usr/bin/docker")
    monkeypatch.setattr(setup.subprocess,"run",lambda *args,**kwargs:SimpleNamespace(returncode=0))
    monkeypatch.setattr(setup,"check_ports",lambda:[])
    monkeypatch.chdir(tmp_path)
    answers=iter(["media.example.test","https://bodycam.example.test"])
    monkeypatch.setattr("builtins.input",lambda prompt:next(answers))
    admin='Admin$"quote\'password123'
    viewer='Viewer$"quote\'password123'
    passwords=iter([admin,admin,viewer,viewer])
    monkeypatch.setattr(setup.getpass,"getpass",lambda prompt:next(passwords))
    setup.main()
    settings=json.loads(setup.CONFIG.read_text())
    media=yaml.safe_load((tmp_path/"data/mediamtx.yml").read_text())
    assert settings["ADMIN_PASSWORD"]==admin and settings["VIEWER_PASSWORD"]==viewer
    assert media["hlsCDNSecret"]==settings["INTERNAL_KEY"]
    assert setup.CONFIG.stat().st_mode&0o777==0o600
    assert (tmp_path/"data").stat().st_mode&0o777==0o700
    assert admin not in setup.ENV.read_text() and viewer not in setup.ENV.read_text()
    assert admin not in capsys.readouterr().out
    saved=setup.CONFIG.read_bytes(),setup.ENV.read_bytes()
    (tmp_path/"data/recordings/keep.mp4").write_bytes(b"existing recording")
    setup.main()  # Asking again would exhaust the input/password iterators and fail.
    assert saved==(setup.CONFIG.read_bytes(),setup.ENV.read_bytes())
    assert (tmp_path/"data/recordings/keep.mp4").read_bytes()==b"existing recording"
