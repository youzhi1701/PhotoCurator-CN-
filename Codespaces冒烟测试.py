#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate GitHub Codespaces forwarding without weakening local request guards."""

import os

os.environ["CODESPACES"] = "true"
os.environ["CODESPACE_NAME"] = "ci-preview"
os.environ["GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN"] = "app.github.dev"
os.environ["PHOTOCURATOR_PORT"] = "5014"

import photo_curator as pc  # noqa: E402

EXPECTED = "ci-preview-5014.app.github.dev"

assert pc.CODESPACES_PUBLIC_HOST == EXPECTED
assert pc.SERVER_HOST == "0.0.0.0"
assert EXPECTED in pc._ALLOWED_HOSTS
assert "evil.example" not in pc._ALLOWED_HOSTS

client = pc.app.test_client()

# Exact GitHub forwarded host is accepted.
r = client.get("/", headers={"Host": EXPECTED})
assert r.status_code == 200, r.status_code

# Same-origin API request is accepted.
r = client.get(
    "/api/shortcuts",
    headers={"Host": EXPECTED, "Origin": f"https://{EXPECTED}"},
)
assert r.status_code == 200, r.status_code
payload = r.get_json()
assert payload["codespaces"] is True

# Unknown forwarded hosts are rejected.
r = client.get("/", headers={"Host": "evil.example"})
assert r.status_code == 403, r.status_code

# Even with the correct Host, a cross-site Origin is rejected.
r = client.get(
    "/api/shortcuts",
    headers={"Host": EXPECTED, "Origin": "https://evil.example"},
)
assert r.status_code == 403, r.status_code

# Direct loopback access remains valid for local health checks.
r = client.get("/", headers={"Host": "127.0.0.1:5014"})
assert r.status_code == 200, r.status_code

print("Codespaces forwarding/security smoke test OK")
