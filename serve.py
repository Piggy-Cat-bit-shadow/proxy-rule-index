#!/usr/bin/env python3
"""Simple static server for the rule-index dist (dev preview only)."""
import http.server
import os
import sys

os.chdir("/Users/jie/Downloads/rule-index/dist")

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
handler = http.server.SimpleHTTPRequestHandler
httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
print(f"serving deploy-site on http://127.0.0.1:{port}")
httpd.serve_forever()
