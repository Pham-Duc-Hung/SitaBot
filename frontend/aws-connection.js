"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const dialog = $("awsConnectionDialog");
  if (!dialog) return;

  const elements = {
    open: $("awsConnectionOpen"),
    badge: $("awsConnectionBadge"),
    close: $("awsConnectionClose"),
    message: $("awsConnectionMessage"),
    current: $("awsConnectionCurrent"),
    account: $("awsConnectedAccount"),
    role: $("awsConnectedRole"),
    externalButton: $("awsExternalIdButton"),
    externalSection: $("awsExternalSection"),
    externalId: $("awsExternalId"),
    copyExternal: $("awsCopyExternalId"),
    trustSection: $("awsTrustSection"),
    trustPolicy: $("awsTrustPolicy"),
    copyTrust: $("awsCopyTrustPolicy"),
    trustUnavailable: $("awsTrustUnavailable"),
    verifyForm: $("awsVerifyForm"),
    roleArn: $("awsRoleArn"),
    verifyButton: $("awsVerifyButton")
  };

  function setMessage(message, tone = "info") {
    elements.message.textContent = message;
    elements.message.dataset.tone = tone;
  }

  function setBadge(label, state) {
    elements.badge.textContent = label;
    elements.badge.dataset.state = state;
  }

  function showConnected(connection) {
    const connected = connection.status === "CONNECTED";
    elements.current.hidden = !connected;
    elements.account.textContent = connected ? connection.account_id || "—" : "";
    elements.role.textContent = connected ? connection.role_arn || "—" : "";
    setBadge(connected ? "AWS: Đã kết nối" : "AWS: Chưa kết nối", connected ? "connected" : "disconnected");
    setMessage(connected ? "SatiBot đã xác minh IAM Role bằng AWS STS." : "Chưa liên kết tài khoản AWS. Hãy lấy ExternalId để bắt đầu.", connected ? "success" : "info");
  }

  function clearSetup() {
    elements.externalSection.hidden = true;
    elements.externalId.value = "";
    elements.trustPolicy.textContent = "";
    elements.roleArn.value = "";
  }

  async function connectionRequest(path, method = "GET", payload) {
    const headers = { Accept: "application/json" };
    if (payload !== undefined) headers["Content-Type"] = "application/json";

    // The login module may provide a Cognito ID token. Never store AWS keys or
    // a token in this page; same-origin session authentication also works.
    const auth = window.SatiBotAuth;
    const token = typeof auth?.getIdToken === "function" ? await auth.getIdToken() : null;
    if (token) headers.Authorization = `Bearer ${token}`;

    const response = await fetch(path, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      ...(payload !== undefined ? { body: JSON.stringify(payload) } : {})
    });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      const error = new Error(`HTTP ${response.status}`);
      error.status = response.status;
      error.code = data?.error_code;
      throw error;
    }
    if (!data || typeof data !== "object") throw new Error("InvalidConnectionResponse");
    return data;
  }

  function showError(error, action) {
    if (error.status === 401) {
      elements.current.hidden = true;
      clearSetup();
      setBadge("AWS: Cần đăng nhập", "unavailable");
      setMessage("Hãy đăng nhập SatiBot trước khi liên kết tài khoản AWS.", "error");
    } else if (error.status === 404) {
      elements.current.hidden = true;
      clearSetup();
      setBadge("AWS: Chưa cấu hình", "unavailable");
      setMessage("Máy chủ chưa mở API liên kết AWS (/api/connections).", "error");
    } else if (error.status === 403 && action === "verify") {
      setMessage("AWS từ chối AssumeRole. Hãy kiểm tra ARN, trust policy, ExternalId và quyền sts:AssumeRole của SatiBot.", "error");
    } else if (error.status === 409 && action === "verify") {
      setMessage("Hãy lấy ExternalId trước khi xác minh IAM Role.", "error");
    } else {
      setMessage(`Không thể ${action === "load" ? "đọc trạng thái" : action === "verify" ? "xác minh Role" : "lấy ExternalId"}. Hãy kiểm tra kết nối và cấu hình máy chủ.`, "error");
    }
  }

  async function loadConnection() {
    try {
      const connection = await connectionRequest("/api/connections");
      clearSetup();
      showConnected(connection);
    } catch (error) {
      showError(error, "load");
    }
  }

  async function getExternalId() {
    elements.externalButton.disabled = true;
    setMessage("Đang lấy ExternalId…");
    try {
      const data = await connectionRequest("/api/connections/external-id", "POST", {});
      if (typeof data.external_id !== "string" || !data.external_id) throw new Error("MissingExternalId");
      elements.externalId.value = data.external_id;
      elements.externalSection.hidden = false;
      const hasPolicy = data.trust_policy && typeof data.trust_policy === "object";
      elements.trustSection.hidden = !hasPolicy;
      elements.trustUnavailable.hidden = Boolean(hasPolicy);
      elements.trustPolicy.textContent = hasPolicy ? JSON.stringify(data.trust_policy, null, 2) : "";
      setMessage(hasPolicy ? "Đã lấy ExternalId. Tạo IAM Role rồi dán Role ARN để xác minh." : "Đã lấy ExternalId; máy chủ chưa cung cấp trust policy. Chưa thể hoàn tất thiết lập Role.", hasPolicy ? "success" : "error");
    } catch (error) {
      showError(error, "external-id");
    } finally {
      elements.externalButton.disabled = false;
    }
  }

  async function verifyRole(event) {
    event.preventDefault();
    const roleArn = elements.roleArn.value.trim();
    if (!/^arn:aws:iam::\d{12}:role\/satibot\/SatiBotAccessRole$/.test(roleArn)) {
      setMessage("Role ARN phải có dạng arn:aws:iam::<AWS_ACCOUNT_ID>:role/satibot/SatiBotAccessRole.", "error");
      return;
    }
    elements.verifyButton.disabled = true;
    setMessage("Đang xác minh IAM Role qua AWS STS…");
    try {
      const result = await connectionRequest("/api/connections/verify", "POST", { role_arn: roleArn });
      if (result.status !== "CONNECTED") throw new Error("InvalidConnectionResponse");
      showConnected({ status: result.status, role_arn: result.role_arn, account_id: roleArn.split(":")[4] });
      await loadConnection();
    } catch (error) {
      showError(error, "verify");
    } finally {
      elements.verifyButton.disabled = false;
    }
  }

  async function copyValue(value) {
    try {
      await navigator.clipboard.writeText(value);
      setMessage("Đã sao chép vào clipboard.", "success");
    } catch {
      setMessage("Không thể sao chép tự động. Hãy chọn và sao chép nội dung thủ công.", "error");
    }
  }

  function openDialog() {
    if (!dialog.open) dialog.showModal();
    loadConnection();
  }

  elements.open.addEventListener("click", openDialog);
  elements.badge.addEventListener("click", openDialog);
  elements.close.addEventListener("click", () => dialog.close());
  elements.externalButton.addEventListener("click", getExternalId);
  elements.verifyForm.addEventListener("submit", verifyRole);
  elements.copyExternal.addEventListener("click", () => copyValue(elements.externalId.value));
  elements.copyTrust.addEventListener("click", () => copyValue(elements.trustPolicy.textContent));
  window.addEventListener("satibot-auth-changed", loadConnection);
  loadConnection();
})();
