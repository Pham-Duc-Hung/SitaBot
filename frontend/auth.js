/** Cognito sign-in for the isolated web preview (authorization code + PKCE). */

const TOKEN_KEY = "satibot.auth.tokens";
const FLOW_KEY = "satibot.auth.flow";
const CLOCK_SKEW_SECONDS = 30;

function cognitoOrigin(config) {
  const origin = new URL(`https://${config.domain}`);
  if (!/^[a-z0-9-]+\.auth\.ap-southeast-1\.amazoncognito\.com$/.test(origin.hostname)) {
    throw new Error("Cognito domain không hợp lệ.");
  }
  return origin;
}

function base64url(bytes) {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function jwtExpiry(token) {
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(payload)).exp || 0;
  } catch {
    return 0;
  }
}

export function chatStorageKey(subject) {
  return typeof subject === "string" && subject
    ? "satibot.chat.sessions.v2." + subject
    : null;
}

export function authorizeUrl(config, state, challenge) {
  const url = new URL("/oauth2/authorize", cognitoOrigin(config));
  url.search = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    scope: "openid email profile",
    redirect_uri: config.redirect_uri,
    state,
    code_challenge_method: "S256",
    code_challenge: challenge
  }).toString();
  return url.toString();
}

export function createBrowserAuth({ win = window, fetchImpl = fetch } = {}) {
  let config = null;
  let initPromise;
  const button = win.document.getElementById("authButton");
  const message = win.document.getElementById("authMessage");

  function report(text) {
    if (message) message.textContent = text;
  }

  function getStoredToken() {
    try {
      const data = JSON.parse(win.sessionStorage.getItem(TOKEN_KEY) || "null");
      if (data?.id_token && jwtExpiry(data.id_token) > Date.now() / 1000 + CLOCK_SKEW_SECONDS) {
        return data.id_token;
      }
    } catch {
      // A malformed or expired browser session is treated as signed out.
    }
    win.sessionStorage.removeItem(TOKEN_KEY);
    return null;
  }

  function refreshButton() {
    if (!button) return;
    button.hidden = !config;
    const label = getStoredToken() ? "Đăng xuất SatiBot" : "Đăng nhập SatiBot";
    const labelElement = button.querySelector?.(".auth-button-label");
    if (labelElement) labelElement.textContent = label;
    else button.textContent = label;
    button.title = label;
    button.setAttribute?.("aria-label", label);
  }

  async function handleCallback() {
    const query = new URLSearchParams(win.location.search);
    const code = query.get("code");
    const state = query.get("state");
    const error = query.get("error");
    if (!code && !error) return;
    query.delete("code"); query.delete("state"); query.delete("error"); query.delete("error_description");
    win.history.replaceState(null, "", `${win.location.pathname}${query.size ? `?${query}` : ""}${win.location.hash}`);
    if (error) throw new Error("Cognito không hoàn tất đăng nhập.");

    const flow = JSON.parse(win.sessionStorage.getItem(FLOW_KEY) || "null");
    win.sessionStorage.removeItem(FLOW_KEY);
    if (!flow?.verifier || !flow?.state || flow.state !== state) {
      throw new Error("Phiên đăng nhập không khớp. Hãy thử lại.");
    }
    const tokenUrl = new URL("/oauth2/token", cognitoOrigin(config));
    const response = await fetchImpl(tokenUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        grant_type: "authorization_code",
        client_id: config.client_id,
        code,
        redirect_uri: config.redirect_uri,
        code_verifier: flow.verifier
      })
    });
    if (!response.ok) throw new Error(`Cognito từ chối mã đăng nhập (HTTP ${response.status}).`);
    const tokens = await response.json();
    if (!tokens.id_token || jwtExpiry(tokens.id_token) <= Date.now() / 1000) {
      throw new Error("Cognito không trả về ID token hợp lệ.");
    }
    win.sessionStorage.setItem(TOKEN_KEY, JSON.stringify({ id_token: tokens.id_token }));
    win.dispatchEvent(new Event("satibot-auth-changed"));
  }

  async function initialize() {
    try {
      const response = await fetchImpl("/site-config.json", { cache: "no-store" });
      if (!response.ok) return;
      if (response.headers?.get?.("content-type")?.includes("text/html")) return;
      const siteConfig = await response.json();
      if (!siteConfig.auth) return;
      config = siteConfig.auth;
      cognitoOrigin(config);
      if (config.redirect_uri !== win.location.origin + "/") {
        throw new Error("Cognito callback không khớp website hiện tại.");
      }
      await handleCallback();
    } catch (error) {
      report(error.message || "Không thể hoàn tất đăng nhập.");
    } finally {
      refreshButton();
    }
  }

  async function signIn() {
    await initPromise;
    if (!config) throw new Error("Website chưa cấu hình Cognito.");
    const state = base64url(win.crypto.getRandomValues(new Uint8Array(24)));
    const verifier = base64url(win.crypto.getRandomValues(new Uint8Array(32)));
    const digest = await win.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
    const challenge = base64url(new Uint8Array(digest));
    win.sessionStorage.setItem(FLOW_KEY, JSON.stringify({ state, verifier }));
    win.location.assign(authorizeUrl(config, state, challenge));
  }

  async function signOut() {
    await initPromise;
    win.sessionStorage.removeItem(TOKEN_KEY);
    win.sessionStorage.removeItem(FLOW_KEY);
    refreshButton();
    win.dispatchEvent(new Event("satibot-auth-changed"));
    if (config) {
      const url = new URL("/logout", cognitoOrigin(config));
      url.search = new URLSearchParams({ client_id: config.client_id, logout_uri: config.redirect_uri }).toString();
      win.location.assign(url.toString());
    }
  }

  if (button) button.addEventListener("click", () => {
    const action = getStoredToken() ? signOut : signIn;
    action().catch(error => report(error.message || "Đăng nhập thất bại."));
  });
  initPromise = initialize();
  return {
    getIdToken: async () => { await initPromise; return getStoredToken(); },
    getUserId: async () => {
      await initPromise;
      const token = getStoredToken();
      if (!token) return null;
      try {
        const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
        return JSON.parse(atob(payload)).sub || null;
      } catch {
        return null;
      }
    },
    signIn,
    signOut
  };
}

if (typeof window !== "undefined") {
  window.SatiBotAuth = createBrowserAuth();
}
