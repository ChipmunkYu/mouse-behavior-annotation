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


def test_nginx_media_stream_candidate_is_narrow_and_cookie_safe():
    config = NGINX.read_text(encoding="utf-8")
    stream = re.search(
        r"location\s+~\s+\^/api/videos/\[0-9\]\+/stream\$\s*\{(?P<body>.*?)\n    \}", config, re.S
    )
    assert stream is not None
    body = stream.group("body")
    # Nginx permits HEAD wherever GET is permitted; every other method is denied.
    assert "limit_except GET" in body and "deny all;" in body
    assert "auth_request" not in body
    assert "proxy_buffering off;" in body
    assert "gzip off;" in body
    assert "proxy_force_ranges" not in body
    assert "access_log" in body
    assert config.index(stream.group(0)) < config.index("location /api/")

    media_format = re.search(r"log_format\s+media_stream\s+(?P<body>.*?);", config, re.S)
    assert media_format is not None
    log_body = media_format.group("body")
    assert "$uri" in log_body
    for forbidden in ("$request ", "$request_uri", "$args", "$http_cookie"):
        assert forbidden not in log_body


def test_nginx_logout_candidate_is_exact_post_only_bypass():
    config = NGINX.read_text(encoding="utf-8")
    logout = re.search(r"location\s+=\s+/api/auth/logout\s*\{(?P<body>.*?)\n    \}", config, re.S)
    assert logout is not None
    body = logout.group("body")
    assert "limit_except POST" in body
    assert "auth_request" not in body
    assert config.index(logout.group(0)) < config.index("location /api/")
