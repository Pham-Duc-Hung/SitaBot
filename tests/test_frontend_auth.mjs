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
  const button = { hidden: true, textContent: "", addEventListener() {} };
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
  assert.equal(button.textContent, "Đăng xuất SatiBot");
  assert.equal(values.has("satibot.auth.flow"), false);
});
