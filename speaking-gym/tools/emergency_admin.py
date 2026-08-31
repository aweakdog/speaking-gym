#!/usr/bin/env python3
import argparse
import hashlib
import hmac
import http.client
import json
import os
import ssl
import urllib.parse

DEFAULT_URL = "https://143.89.46.41:1511"
CONFIG_DIR = os.path.expanduser("~/Library/Application Support/Speaking Gym")
TOKEN_FILE = os.path.join(CONFIG_DIR, "admin-token")
FINGERPRINT_FILE = os.path.join(CONFIG_DIR, "server-cert.sha256")


def read_secret(path):
    try:
        value = open(path, encoding="utf-8").read().strip()
    except OSError as e:
        raise SystemExit("Missing %s: %s" % (path, e))
    if not value:
        raise SystemExit("Empty credential file: " + path)
    return value


def request(method, path, body=None):
    url = urllib.parse.urlparse(os.environ.get("SG_ADMIN_URL", DEFAULT_URL))
    if url.scheme != "https" or not url.hostname:
        raise SystemExit("SG_ADMIN_URL must be an https URL")
    expected = read_secret(FINGERPRINT_FILE).replace(":", "").lower()
    if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise SystemExit("Invalid certificate fingerprint file")
    token = read_secret(TOKEN_FILE)
    context = ssl._create_unverified_context()
    conn = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=190, context=context)
    conn.connect()
    actual = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
    if not hmac.compare_digest(actual, expected):
        conn.close()
        raise SystemExit("TLS certificate fingerprint mismatch; refusing connection")
    raw = json.dumps(body).encode() if body is not None else None
    headers = {
        "X-Speaking-Gym-Admin": token,
        "Accept": "application/json",
        "User-Agent": "speaking-gym-emergency-admin/1",
    }
    if raw is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(raw))
    conn.request(method, path, body=raw, headers=headers)
    response = conn.getresponse()
    payload = response.read(2_000_000)
    conn.close()
    try:
        data = json.loads(payload.decode())
    except Exception:
        raise SystemExit("Server returned a non-JSON response (HTTP %d)" % response.status)
    if response.status >= 400:
        raise SystemExit("HTTP %d: %s" % (response.status, data.get("error", "request failed")))
    return data


def main():
    parser = argparse.ArgumentParser(description="Speaking Gym VPN-fallback admin client")
    parser.add_argument("action", choices=("status", "health", "logs", "backup", "update", "restart"))
    args = parser.parse_args()
    if args.action in ("status", "logs"):
        data = request("GET", "/api/admin/" + args.action)
    else:
        data = request("POST", "/api/admin/action", {"action": args.action})
    if args.action == "logs":
        print("\n".join(data.get("lines", [])))
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    if args.action == "restart":
        print("Restart scheduled. Wait a few seconds, then run: emergency_admin.py status")


if __name__ == "__main__":
    main()
