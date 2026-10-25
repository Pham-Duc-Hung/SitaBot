"""
Author: Trịnh Thiên Quang
Máy chủ web demo cục bộ để kiểm thử giao diện ChatOps trực quan trên trình duyệt.
"""

import http.server
import json
import os
import socketserver
import sys

# Thêm path đến backend và dispatcher
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DISPATCHER_PATH = os.path.join(PROJECT_ROOT, "backend", "functions", "dispatcher")
for p in (PROJECT_ROOT, DISPATCHER_PATH):
    if p not in sys.path:
        sys.path.insert(0, p)

# Tự động nạp cấu hình từ .env nếu chưa có
env_file = os.path.join(PROJECT_ROOT, ".env")
if os.path.exists(env_file):
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v

from backend.functions.chatbot.index import lambda_handler

PORT = 8080

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SatiBot - Trợ lý ChatOps Quản trị AWS</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --bubble-bot: #334155;
      --bubble-user: #2563eb;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --warning: #f59e0b;
      --success: #10b981;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      display: flex;
      justify-content: center;
      align-items: center;
      height: 100vh;
    }
    .chat-container {
      width: 95%;
      max-width: 850px;
      height: 92vh;
      background-color: var(--card-bg);
      border-radius: 16px;
      display: flex;
      flex-direction: column;
      box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.5);
      border: 1px solid #334155;
      overflow: hidden;
    }
    .chat-header {
      padding: 16px 24px;
      background: #111827;
      border-bottom: 1px solid #334155;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .header-title { display: flex; align-items: center; gap: 12px; }
    .bot-avatar {
      width: 42px; height: 42px; border-radius: 50%;
      background: linear-gradient(135deg, #0ea5e9, #6366f1);
      display: flex; align-items: center; justify-content: center;
      font-weight: bold; font-size: 20px;
    }
    .badges { display: flex; gap: 8px; }
    .badge {
      font-size: 11px; padding: 4px 10px; border-radius: 12px;
      background: #0284c7; color: white; font-weight: 500;
    }
    .badge-aws { background: #d97706; }
    .chat-messages {
      flex: 1; padding: 20px; overflow-y: auto;
      display: flex; flex-direction: column; gap: 16px;
    }
    .message-row { display: flex; gap: 12px; max-width: 85%; }
    .message-row.user { align-self: flex-end; flex-direction: row-reverse; }
    .bubble {
      padding: 12px 16px; border-radius: 16px; font-size: 14.5px;
      line-height: 1.5; word-break: break-word;
    }
    .message-row.bot .bubble { background-color: var(--bubble-bot); border-bottom-left-radius: 4px; }
    .message-row.user .bubble { background-color: var(--bubble-user); border-bottom-right-radius: 4px; color: white; }
    .action-card {
      margin-top: 10px; padding: 14px; border-radius: 10px;
      background: #0f172a; border: 1px dashed var(--warning);
    }
    .action-card.success { border-color: var(--success); }
    .action-buttons { display: flex; gap: 10px; margin-top: 12px; }
    .btn {
      padding: 8px 16px; border-radius: 8px; border: none;
      cursor: pointer; font-size: 13px; font-weight: 600; transition: 0.2s;
    }
    .btn-confirm { background: var(--success); color: white; }
    .btn-cancel { background: #475569; color: white; }
    .btn:hover { opacity: 0.9; }
    .chat-input-area {
      padding: 16px 20px; background: #111827; border-top: 1px solid #334155;
      display: flex; gap: 10px;
    }
    .chat-input {
      flex: 1; padding: 12px 18px; border-radius: 24px;
      border: 1px solid #475569; background: #1e293b; color: white;
      font-size: 14.5px; outline: none;
    }
    .chat-input:focus { border-color: var(--accent); }
    .send-btn {
      padding: 12px 24px; border-radius: 24px; background: var(--bubble-user);
      color: white; border: none; font-weight: 600; cursor: pointer;
    }
    .quick-prompts {
      display: flex; gap: 8px; padding: 8px 20px 0; overflow-x: auto;
    }
    .quick-chip {
      font-size: 12px; background: #1e293b; color: var(--accent);
      padding: 5px 12px; border-radius: 12px; cursor: pointer;
      border: 1px solid #334155; white-space: nowrap;
    }
    .quick-chip:hover { background: #334155; }
    pre { background: #0b0f19; padding: 8px; border-radius: 6px; font-size: 12px; overflow-x: auto; margin-top: 6px; }
    .agent-steps {
      background: #090e17; border-left: 3px solid #6366f1; border-radius: 8px;
      padding: 12px 16px; margin-bottom: 12px; font-size: 13px;
    }
    .agent-step-item {
      margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px dashed #1e293b; line-height: 1.5;
    }
    .agent-step-item:last-child { margin-bottom: 0; padding-bottom: 0; border-bottom: none; }
    .agent-tag {
      display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 11px;
      font-weight: 700; text-transform: uppercase; margin-right: 6px;
    }
    .tag-thought { background: #312e81; color: #c7d2fe; }
    .tag-action { background: #065f46; color: #a7f3d0; }
    .tag-obs { background: #713f12; color: #fde047; }
  </style>
</head>
<body>
  <div class="chat-container">
    <div class="chat-header">
      <div class="header-title">
        <div class="bot-avatar">🤖</div>
        <div>
          <h3>SatiBot - AI ChatOps</h3>
          <p style="font-size: 12px; color: var(--text-muted);">Trợ lý tương tác tài nguyên AWS thông minh</p>
        </div>
      </div>
      <div class="badges">
        <span class="badge">Gemini 3.5 Flash Lite</span>
        <span class="badge badge-aws">AWS us-east-1</span>
      </div>
    </div>

    <div class="quick-prompts">
      <div class="quick-chip" onclick="fillInput('Chẩn đoán sức khỏe và rủi ro các hàm Lambda trong tài khoản giúp tôi')">🩺 Chẩn đoán sức khỏe (Health Check)</div>
      <div class="quick-chip" onclick="fillInput('Hàm test-lambda dạo này chạy chậm, hãy kiểm tra và đề xuất tối ưu giúp tôi')">⚡ Kiểm tra & Tối ưu test-lambda</div>
      <div class="quick-chip" onclick="fillInput('Liệt kê danh sách các hàm Lambda trong tài khoản')">📋 Liệt kê hàm</div>
      <div class="quick-chip" onclick="fillInput('Tạo đường dẫn Function URL công khai cho hàm test-lambda cho phép truy cập từ https://satibot.com')">🌐 Tạo Function URL</div>
      <div class="quick-chip" onclick="fillInput('Khóa khẩn cấp hàm test-lambda để chống quá tải')">🛑 Khóa khẩn cấp</div>
      <div class="quick-chip" onclick="fillInput('Mở khóa khẩn cấp cho hàm test-lambda hoạt động trở lại')">🔓 Mở khóa</div>
    </div>

    <div class="chat-messages" id="messages">
      <div class="message-row bot">
        <div class="bubble">
          Xin chào Trịnh Thiên Quang! Tôi là <strong>SatiBot</strong>. Tôi đã sẵn sàng kết nối trực tiếp với tài khoản AWS của bạn qua Gemini 3.5 Flash Lite.<br><br>
          Bạn có thể thử nhắn các câu lệnh quản trị hoặc chọn gợi ý bên trên nhé!
        </div>
      </div>
    </div>

    <div class="chat-input-area">
      <input type="text" id="userInput" class="chat-input" placeholder="Nhập yêu cầu bằng tiếng Việt (vd: Kiểm tra hàm Lambda, chạy thử hàm...)" onkeydown="handleKeyDown(event)">
      <button class="send-btn" id="sendBtn" onclick="sendMessage()">Gửi</button>
    </div>
  </div>

  <script>
    const messagesDiv = document.getElementById('messages');
    const input = document.getElementById('userInput');
    const sendBtn = document.getElementById('sendBtn');
    let conversationHistory = [];

    function handleKeyDown(event) {
      if (event.key === 'Enter' && !event.isComposing && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    }

    function fillInput(text) {
      input.value = text;
      input.focus();
    }

    function addMessage(sender, htmlContent) {
      const row = document.createElement('div');
      row.className = `message-row ${sender}`;
      row.innerHTML = `<div class="bubble">${htmlContent}</div>`;
      messagesDiv.appendChild(row);
      messagesDiv.scrollTop = messagesDiv.scrollHeight;
    }

    async function sendMessage() {
      const text = input.value.trim();
      if (!text) return;
      input.value = '';
      sendBtn.disabled = true;
      sendBtn.style.opacity = '0.6';
      sendBtn.innerText = 'Đang xử lý...';
      addMessage('user', text);

      // Loading bubble
      const loadingId = 'loading-' + Date.now();
      const loadingRow = document.createElement('div');
      loadingRow.id = loadingId;
      loadingRow.className = 'message-row bot';
      loadingRow.innerHTML = '<div class="bubble">⏳ <em>SatiBot đang phân tích câu lệnh và xử lý trên AWS...</em></div>';
      messagesDiv.appendChild(loadingRow);
      messagesDiv.scrollTop = messagesDiv.scrollHeight;

      try {
        const resp = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ message: text, history: conversationHistory })
        });
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} - ${resp.statusText}`);
        }
        const res = await resp.json();
        const loadElem = document.getElementById(loadingId);
        if (loadElem) loadElem.remove();
        renderBotResponse(res);

        // Lưu ngữ cảnh hội thoại
        conversationHistory.push({ role: 'user', content: text });
        if (res.message) {
          conversationHistory.push({ role: 'assistant', content: res.message });
        }
        if (conversationHistory.length > 10) {
          conversationHistory = conversationHistory.slice(-10);
        }
      } catch (err) {
        const loadElem = document.getElementById(loadingId);
        if (loadElem) loadElem.remove();
        addMessage('bot', `❌ Lỗi kết nối (Cổng ${window.location.port || '8080'}): ${err.message}. Hãy kiểm tra xem server demo có đang chạy trên terminal không.`);
      } finally {
        sendBtn.disabled = false;
        sendBtn.style.opacity = '1';
        sendBtn.innerText = 'Gửi';
        input.focus();
      }
    }

    let currentPending = null;

    function formatInline(str) {
      return str
        .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
        .replace(/\*(.*?)\*/g, '<em>$1</em>')
        .replace(/`(.*?)`/g, '<code style="background: #1e293b; padding: 2px 6px; border-radius: 4px; color: #f472b6;">$1</code>');
    }

    function formatMarkdown(text) {
      if (!text) return '';
      const lines = String(text).split(/\r?\n/);
      const htmlLines = [];
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        if (line.startsWith('### ')) {
          htmlLines.push('<h4 style="margin: 12px 0 6px 0; color: #38bdf8; font-size: 15px;">' + formatInline(line.slice(4)) + '</h4>');
        } else if (line.startsWith('#### ')) {
          htmlLines.push('<h5 style="margin: 10px 0 4px 0; color: #818cf8; font-size: 13.5px;">' + formatInline(line.slice(5)) + '</h5>');
        } else if (line.startsWith('- ') || line.startsWith('* ')) {
          htmlLines.push('<div style="margin-left: 14px;">• ' + formatInline(line.slice(2)) + '</div>');
        } else if (line.trim() === '---') {
          htmlLines.push('<hr style="border: 0; border-top: 1px solid #334155; margin: 10px 0;">');
        } else if (line.trim() === '') {
          htmlLines.push('<div style="height: 8px;"></div>');
        } else {
          htmlLines.push('<div>' + formatInline(line) + '</div>');
        }
      }
      return htmlLines.join('');
    }

    function renderBotResponse(res) {
      let stepsHtml = '';
      if (res.steps && res.steps.length > 0) {
        stepsHtml = `
          <div class="agent-steps">
            <div style="font-weight: 700; color: #a5b4fc; margin-bottom: 8px;">🧠 Chu trình suy luận của Agent (ReAct Loop):</div>
            ${res.steps.map(s => `
              <div class="agent-step-item">
                <div style="margin-bottom: 3px;"><span class="agent-tag tag-thought">Thought</span>${s.thought}</div>
                <div style="margin-bottom: 3px;"><span class="agent-tag tag-action">Action</span><code>${s.action}</code> <span style="color:#94a3b8; font-size:11px;">${JSON.stringify(s.parameters || {})}</span></div>
                <div><span class="agent-tag tag-obs">Observation</span><em style="color:#fde047;">${s.observation}</em></div>
              </div>
            `).join('')}
          </div>
        `;
      }

      if (res.status === 'CONFIRMATION_REQUIRED') {
        currentPending = {
          tool_name: res.tool_name,
          parameters: res.parameters
        };
        const paramsStr = JSON.stringify(res.parameters, null, 2);
        const msgFormatted = formatMarkdown(res.message);
        const html = `
          ${stepsHtml}
          <div>${msgFormatted}</div>
          <div class="action-card">
            <strong>⚠️ Thao tác yêu cầu xác nhận:</strong> <code>${res.tool_name}</code>
            <pre>${paramsStr}</pre>
            <div class="action-buttons">
              <button class="btn btn-confirm" onclick="executeCurrentPending()">✅ Xác nhận thực thi</button>
              <button class="btn btn-cancel" onclick="cancelAction()">❌ Hủy bỏ</button>
            </div>
          </div>
        `;
        addMessage('bot', html);
      } else if (res.type === 'action_executed') {
        let extra = '';
        if (res.data && res.data.functions) {
          extra = '<ul style="margin: 8px 0 0 20px;">' + 
            res.data.functions.map(f => `<li><strong>${f.function_name}</strong> (${f.runtime}, RAM ${f.memory_mb}MB, Timeout ${f.timeout_seconds}s)</li>`).join('') + 
            '</ul>';
        } else if (res.data && res.data.response_payload) {
          extra = '<pre>' + JSON.stringify(res.data.response_payload, null, 2) + '</pre>';
        }
        const isSuccess = res.status === 'SUCCESS';
        const icon = isSuccess ? '✅' : '❌';
        addMessage('bot', `<div>${stepsHtml}${icon} <strong>${formatMarkdown(res.message)}</strong>${extra}</div>`);
      } else {
        addMessage('bot', `<div>${stepsHtml}${formatMarkdown(res.message || JSON.stringify(res))}</div>`);
      }
    }

    async function executeCurrentPending() {
      if (!currentPending) return;
      const { tool_name, parameters } = currentPending;
      currentPending = null;
      addMessage('user', '✅ Tôi xác nhận thực hiện thao tác.');
      try {
        const resp = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            confirmed: true,
            service: 'lambda',
            tool_name: tool_name,
            parameters: parameters
          })
        });
        const res = await resp.json();
        renderBotResponse(res);
      } catch (err) {
        addMessage('bot', '❌ Lỗi khi thực thi: ' + err.message);
      }
    }

    function cancelAction() {
      currentPending = null;
      addMessage('user', '❌ Tôi hủy bỏ thao tác.');
      addMessage('bot', 'Đã hủy bỏ thao tác an toàn. Không có tài nguyên nào bị thay đổi.');
    }
  </script>
</body>
</html>
"""


class ChatRequestHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

    def do_POST(self):
        if self.path == "/api/chat":
            content_length = int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_length)
            try:
                payload = json.loads(body_bytes.decode("utf-8"))
            except Exception:
                payload = {}

            # Gọi thẳng vào Chatbot Lambda Backend
            response = lambda_handler(payload, None)

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Giảm ồn log
        pass


def run():
    socketserver.TCPServer.allow_reuse_address = True
    port = PORT
    httpd = None
    for p in range(PORT, PORT + 10):
        try:
            httpd = socketserver.TCPServer(("", p), ChatRequestHandler)
            port = p
            break
        except OSError:
            continue

    if not httpd:
        print(f"Lỗi: Các cổng từ {PORT} đến {PORT + 9} đều đang bận.")
        return

    with httpd:
        print("\n" + "=" * 65)
        print("🚀 SATIBOT WEB CHAT ĐANG CHẠY TRỰC QUAN TẠI:")
        print(f"👉 http://localhost:{port}")
        print("=" * 65)
        print("Mở đường link trên bằng Google Chrome để trải nghiệm chat như web thật!")
        print("Bấm Ctrl + C trong terminal nếu muốn dừng.\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nĐã tắt máy chủ demo.")


if __name__ == "__main__":
    run()

