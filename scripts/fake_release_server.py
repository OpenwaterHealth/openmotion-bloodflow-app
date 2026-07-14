#!/usr/bin/env python3
"""Local fake GitHub-releases server for end-to-end in-app-update testing.

Serves a ``releases/latest`` JSON that advertises a newer version and points
its asset ``browser_download_url`` back at this server, plus the bundle file
itself. Nothing leaves the machine — point an old build's ``updateApiUrl``
config at ``http://127.0.0.1:<port>/releases/latest`` and the whole
detect -> download -> verify -> install -> relaunch flow runs over loopback.

Usage:
    python scripts/fake_release_server.py \
        --bundle build/installer/Open-Motion-Research-Setup-1.3.1.exe \
        --tag 1.3.1 --port 8077

Then the old build's bundled config/app_config.json must contain:
    "updateApiUrl": "http://127.0.0.1:8077/releases/latest"
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def build_handler(bundle_path: str, tag: str, host_port: str):
    asset_name = os.path.basename(bundle_path)
    download_url = f"http://{host_port}/{asset_name}"
    latest_json = json.dumps(
        {
            "tag_name": tag,
            "name": tag,
            "html_url": f"http://{host_port}/",
            "assets": [
                {
                    "name": asset_name,
                    "browser_download_url": download_url,
                }
            ],
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[fake-release] {self.address_string()} {fmt % args}")

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/releases/latest"):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(latest_json)))
                self.end_headers()
                self.wfile.write(latest_json)
            elif path == f"/{asset_name}":
                size = os.path.getsize(bundle_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with open(bundle_path, "rb") as f:
                    while True:
                        chunk = f.read(1 << 20)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", required=True, help="path to the .exe bundle to serve")
    ap.add_argument("--tag", default="1.3.1", help="release tag to advertise")
    ap.add_argument("--port", type=int, default=8077)
    ap.add_argument("--host", default="127.0.0.1", help="bind/advertise host")
    args = ap.parse_args()

    if not os.path.isfile(args.bundle):
        raise SystemExit(f"bundle not found: {args.bundle}")

    host_port = f"{args.host}:{args.port}"
    handler = build_handler(args.bundle, args.tag, host_port)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"[fake-release] serving tag {args.tag} ({os.path.basename(args.bundle)})")
    print(f"[fake-release]   releases/latest -> http://{host_port}/releases/latest")
    print(f"[fake-release]   set the old build's updateApiUrl to that URL")
    print("[fake-release] Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[fake-release] stopped")


if __name__ == "__main__":
    main()
