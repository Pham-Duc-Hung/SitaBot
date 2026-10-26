"""
Author: Trịnh Thiên Quang
Cung cấp máy chủ điều khiển cục bộ Agent Console và chuyển tiếp gọi Lambda.
"""

import http.server
import json
import os
import socketserver
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    import boto3  # noqa: F401
except ImportError:
    conda_py = Path("/home/sora/miniconda3/envs/chatbot/bin/python")
    if conda_py.exists() and sys.executable != str(conda_py):
        os.execv(str(conda_py), [str(conda_py)] + sys.argv)

WEB_ROOT = PROJECT_ROOT / "frontend"
DISPATCHER_PATH = PROJECT_ROOT / "backend" / "functions" / "dispatcher"
for path in (str(PROJECT_ROOT), str(DISPATCHER_PATH)):
    if path not in sys.path:
        sys.path.insert(0, path)

env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    with env_file.open(encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = value.strip().strip("'\"")

from backend.functions.chatbot.index import lambda_handler  # noqa: E402

PORT = 8080


class AgentConsoleHandler(http.server.SimpleHTTPRequestHandler):
    """Serve the local console and bridge requests to the real chatbot Lambda."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self):
        if self.path == "/api/health":
            tool_count = 0
            try:
                from backend.functions.chatbot.index import _get_lambda_tool_schemas
                tool_count = len(_get_lambda_tool_schemas())
            except Exception:
                pass
            self._send_json({
                "status": "ready",
                "region": os.environ.get("AWS_REGION", "us-east-1"),
                "model": os.environ.get("LLM_MODEL", "configured model"),
                "llm_configured": bool(os.environ.get("LLM_API_KEY")),
                "tools_loaded": tool_count,
            })
            return
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1_000_000:
                raise ValueError("Request body phải từ 1 byte đến 1 MB.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Payload phải là JSON object.")
            import importlib
            import backend.functions.chatbot.index as chatbot_mod
            import backend.functions.chatbot.agent_executor as agent_mod
            importlib.reload(agent_mod)
            importlib.reload(chatbot_mod)
            self._send_json(chatbot_mod.lambda_handler(payload, None))
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json({"status": "ERROR", "message": str(exc)}, status=400)
        except Exception as exc:
            self._send_json({"status": "ERROR", "message": f"Agent backend lỗi: {exc}"}, status=500)

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if self.path.startswith("/api/") or self.path.endswith((".js", ".css", ".html")) or self.path == "/":
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-SatiBot-Server", "local-current-worktree")
        super().end_headers()

    def log_message(self, format, *args):
        pass


def run():
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    server = None
    port = PORT
    for candidate in range(PORT, PORT + 10):
        try:
            server = socketserver.ThreadingTCPServer(("127.0.0.1", candidate), AgentConsoleHandler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"Các cổng {PORT}-{PORT + 9} đều đang bận.")
    if port != PORT:
        print(f"⚠️ Lưu ý: Cổng {PORT} đã có tiến trình khác chạy. Đang mở cổng phụ: http://127.0.0.1:{port}")
    print(f"SatiBot Agent Console: http://127.0.0.1:{port}")
    print("Nhấn Ctrl+C để dừng.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
