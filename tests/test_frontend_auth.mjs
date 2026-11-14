import assert from "node:assert/strict";
import test from "node:test";
import { authorizeUrl, createBrowserAuth } from "../frontend/auth.js";

const config = {
  domain: "example.auth.ap-southeast-1.amazoncognito.com",
  client_id: "browser-client",
  redirect_uri: "https://preview.cloudfront.net/"
};

test("Cognito authorization uses code flow, PKCE and the preview callback", () => {
  const url = new URL(authorizeUrl(config, "random-state", "challenge"));
  assert.equal(url.pathname, "/oauth2/authorize");
  assert.equal(url.searchParams.get("response_type"), "code");
  assert.equal(url.searchParams.get("code_challenge_method"), "S256");
  assert.equal(url.searchParams.get("state"), "random-state");
  assert.equal(url.searchParams.get("redirect_uri"), config.redirect_uri);
});

test("Cognito callback verifies state, clears URL code and stores ID token for API calls", async () => {
  const values = new Map([["satibot.auth.flow", JSON.stringify({ state: "expected", verifier: "secret-verifier" })]]);
  const changes = [];
  const payload = Buffer.from(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + 3600 })).toString("base64url");
  const idToken = `header.${payload}.signature`;
  const label = { textContent: "Đăng nhập SatiBot" };
  const button = {
    hidden: true,
    title: "",
    attributes: {},
    querySelector: selector => selector === ".auth-button-label" ? label : null,
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener() {}
  };
  const win = {
    document: { getElementById: id => id === "authButton" ? button : null },
    location: {
      origin: "https://preview.cloudfront.net",
      pathname: "/",
      search: "?code=one-time-code&state=expected",
      hash: "",
    },
    history: { replaceState(_state, _title, path) { changes.push(path); } },
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key)
    },
    dispatchEvent() {}
  };
  const requests = [];
  const fetchImpl = async (url, options) => {
    requests.push({ url: String(url), options });
    if (url === "/site-config.json") return { ok: true, json: async () => ({ auth: config }) };
    return { ok: true, json: async () => ({ id_token: idToken }) };
  };
  const auth = createBrowserAuth({ win, fetchImpl });
  assert.equal(await auth.getIdToken(), idToken);
  assert.deepEqual(changes, ["/"]);
  assert.equal(new URLSearchParams(requests[1].options.body).get("code_verifier"), "secret-verifier");
  assert.equal(label.textContent, "Đăng xuất SatiBot");
  assert.equal(button.attributes["aria-label"], "Đăng xuất SatiBot");
  assert.equal(values.has("satibot.auth.flow"), false);
});

test("local Vite HTML fallback does not display a JSON parsing error", async () => {
  const button = { hidden: true, addEventListener() {} };
  const message = { textContent: "" };
  const win = {
    document: { getElementById: id => ({ authButton: button, authMessage: message })[id] },
    location: { search: "" },
    sessionStorage: { getItem: () => null, removeItem() {} }
  };
  const auth = createBrowserAuth({ win, fetchImpl: async () => ({
    ok: true,
    headers: { get: () => "text/html; charset=utf-8" },
    json: async () => { throw new Error("Unexpected token '<'"); }
  }) });
  assert.equal(await auth.getIdToken(), null);
  assert.equal(button.hidden, true);
  assert.equal(message.textContent, "");
});


test("chat cache is isolated by Cognito subject and unavailable after logout", async () => {
  const { chatStorageKey } = await import("../frontend/auth.js");
  const cache = new Map([[chatStorageKey("alice"), "private conversation"]]);
  assert.equal(cache.get(chatStorageKey("bob")), undefined);
  assert.equal(chatStorageKey(null), null);
  assert.equal(chatStorageKey(""), null);
  assert.notEqual(chatStorageKey("alice"), "satibot.chat.sessions.v1");
});
