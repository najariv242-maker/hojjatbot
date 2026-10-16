import os
import time
import threading
import logging
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running 24/7 on Render!")

    def log_message(self, format, *args):
        # Silence access logs to keep terminal clean
        return

def run_http_server(port: int):
    try:
        server_address = ("0.0.0.0", port)
        httpd = HTTPServer(server_address, HealthCheckHandler)
        logger.info(f"Health-check server running on port {port}")
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health-check server on port {port}: {e}")

def ping_loop(target_url: str, interval: int = 300):
    logger.info(f"Keep-alive self-ping started for {target_url} every {interval}s")
    while True:
        time.sleep(interval)
        try:
            req = urllib.request.Request(
                target_url,
                headers={"User-Agent": "Render-KeepAlive-Bot/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                logger.info(f"Keep-alive ping to {target_url} succeeded (status: {resp.status})")
        except Exception as e:
            logger.warning(f"Keep-alive ping to {target_url} notice: {e}")

def start_keep_alive(port: int = 10000, ping_url: str = ""):
    # 1. Start HTTP server thread for Render Web Service binding
    server_thread = threading.Thread(target=run_http_server, args=(port,), daemon=True)
    server_thread.start()

    # 2. If target URL provided or detected, start 5-minute ping thread
    if ping_url:
        ping_thread = threading.Thread(target=ping_loop, args=(ping_url, 300), daemon=True)
        ping_thread.start()
    else:
        # Default self-ping on localhost to ensure local activity
        local_url = f"http://127.0.0.1:{port}/"
        ping_thread = threading.Thread(target=ping_loop, args=(local_url, 300), daemon=True)
        ping_thread.start()
