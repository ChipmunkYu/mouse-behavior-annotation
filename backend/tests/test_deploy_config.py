from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
NGINX = ROOT / "deploy" / "nginx" / "mouse-annotation.conf"
SERVICE = ROOT / "deploy" / "mouse-annotation.service"


def test_nginx_upload_regex_covers_only_actual_upload_routes():
    config = NGINX.read_text(encoding="utf-8")
    match = re.search(r"location ~ (\^/api/projects/[^ ]+) \{", config)
    assert match is not None
    pattern = re.compile(match.group(1))
    expected = [
        "/api/projects/1/videos/upload",
        "/api/projects/1/video-import-batches/2/files/video",
        "/api/projects/1/video-import-batches/2/files/tracks",
        "/api/projects/1/video-import-batches/2/files/metadata",
        "/api/projects/1/videos/2/detection-imports",
    ]
    assert all(pattern.fullmatch(path) for path in expected)
    assert not pattern.fullmatch("/api/projects/1/videos")
    assert not pattern.fullmatch("/api/projects/1/video-import-batches/2/complete")


def test_nginx_auth_limits_hosts_and_buffering_contract():
    config = NGINX.read_text(encoding="utf-8")
    auth_block = re.search(r"location = /_auth \{(?P<body>.*?)\n    \}", config, re.S)
    assert auth_block is not None
    auth_body = auth_block.group("body")
    assert "internal;" in auth_body
    assert "client_max_body_size 0;" in auth_body
    assert "proxy_pass_request_body off;" in auth_body
    assert 'proxy_set_header Content-Length "";' in auth_body
    upload_block = re.search(
        r"location ~ \^/api/projects/[^ ]+ \{(?P<body>.*?)\n    \}", config, re.S
    )
    api_block = re.search(r"location /api/ \{(?P<body>.*?)\n    \}", config, re.S)
    assert upload_block is not None and "client_max_body_size 20g;" in upload_block.group("body")
    assert api_block is not None and "client_max_body_size 10m;" in api_block.group("body")
    assert "limit_conn_zone $binary_remote_addr zone=upload_per_ip:10m;" in config
    assert "return 308 https://jerrylab.xyz$request_uri;" in config
    assert "location = /api/health" in config
    assert "location = /api/auth/login" in config
    assert "location = /_auth" in config and "internal;" in config
    assert "proxy_pass http://127.0.0.1:8000/api/auth/me;" in config
    assert "proxy_set_header Host jerrylab.xyz;" in config
    assert config.count("proxy_request_buffering off;") == 1
    assert "client_max_body_size 20g;" in config
    assert "client_max_body_size 10m;" in config
    assert "limit_conn upload_per_ip 2;" in config
    assert "frame-ancestors 'none'" in config
    assert "add_header X-Frame-Options DENY always;" in config
    assert "Strict-Transport-Security" not in config


def test_systemd_uses_data_disk_multipart_temp_directory():
    service = SERVICE.read_text(encoding="utf-8")
    assert "Environment=TMPDIR=/data/mouse-annotation/tmp" in service
    assert "ExecStartPre=/usr/bin/test -d /data/mouse-annotation/tmp" in service
    assert "UMask=0027" in service
    assert "/NVme" not in service
