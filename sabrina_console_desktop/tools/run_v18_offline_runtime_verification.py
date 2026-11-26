#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sabrina Offline Sandbox Before/After Runtime Verification Suite (v18)
=====================================================================
Deploys isolated `unpatched` and `patched` copies of the extracted packages inside
`Sabrina/artifacts/tool_sandbox/bugcrowd_static_audit_20261116/runtime_verification/`
and executes deterministic runtime tests with ZERO external network traffic:

  1. `okta/okta-jwt-verifier-python` (`v0.5.0`):
     - Real RS256 ephemeral RSA keypair + `BaseJWTVerifier.verify_id_token_sync` / `verify_id_token`
     - Real `JWTUtils.verify_expiration(token, leeway=0)`
  2. `zendesk/zcli` (`@zendesk/zcli-apps 2.1.0` & `@zendesk/zcli-core 2.0.0`):
     - Real Express `/app.json` server bound on ephemeral `127.0.0.1:0` with dummy `"secure": true` config
     - Real `oauth.js` `listen` socket binding inspection (`0.0.0.0`/`::` vs `127.0.0.1`)
  3. `auth0/nextjs-auth0` (`v4.33.0`):
     - Real `Fetcher.fetchWithAuth` (`dist/server/fetcher.js`) with offline `fetch` interceptor
     - Real `transformTargetUrl` (`dist/utils/proxy.js`) + HTTP URL parser comparison
     - Real `deleteChunkedCookie` (`dist/server/cookies.js`) with `@edge-runtime/cookies`
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import py_compile
import shutil
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

SCRIPT_DIR = Path(__file__).resolve().parent
SABRINA_ROOT = SCRIPT_DIR.parent
SANDBOX_BASE = SABRINA_ROOT / "artifacts" / "tool_sandbox" / "bugcrowd_static_audit_20261116"
RUNTIME_DIR = SANDBOX_BASE / "runtime_verification"
OUT_JSON = SABRINA_ROOT / "artifacts" / "collective-repair-20261108" / "offline_runtime_verification_v18.json"


def install_offline_okta_stubs() -> None:
    """Stub optional HTTP cache/retry transport imports so zero external HTTP code can run."""
    acache = types.ModuleType("acachecontrol")
    acache_sub = types.ModuleType("acachecontrol.cache")

    class DummyAsyncCache:
        def clear_cache(self):
            pass

    class DummyAsyncCacheControl:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise RuntimeError("Offline sandbox: external HTTP blocked")

        async def __aexit__(self, *a):
            pass

    acache.AsyncCacheControl = DummyAsyncCacheControl
    acache_sub.AsyncCache = DummyAsyncCache
    sys.modules["acachecontrol"] = acache
    sys.modules["acachecontrol.cache"] = acache_sub

    retry_pkg = types.ModuleType("retry")
    retry_api = types.ModuleType("retry.api")

    def dummy_retry_call(f, fargs=(), fkwargs=None, **kw):
        return f(*fargs, **(fkwargs or {}))

    retry_api.retry_call = dummy_retry_call
    sys.modules["retry"] = retry_pkg
    sys.modules["retry.api"] = retry_api


def load_package_from_dir(pkg_name: str, pkg_dir: Path):
    spec = importlib.util.spec_from_file_location(
        pkg_name,
        pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def verify_okta_python_runtime() -> Dict[str, Any]:
    install_offline_okta_stubs()
    src_pkg = (
        SANDBOX_BASE
        / "repos"
        / "okta-jwt-verifier-python"
        / "okta_jwt_verifier-0.5.0"
        / "okta_jwt_verifier"
    )
    unpatched_dir = RUNTIME_DIR / "py_unpatched" / "okta_jwt_verifier"
    patched_dir = RUNTIME_DIR / "py_patched" / "okta_jwt_verifier"
    for d in (unpatched_dir, patched_dir):
        if d.exists():
            shutil.rmtree(d)
        shutil.copytree(src_pkg, d)

    # Apply defensive patches to py_patched
    jv_file = patched_dir / "jwt_verifier.py"
    jv_txt = jv_file.read_text(encoding="utf-8")
    old_nonce_block = (
        "            if 'nonce' in claims and claims['nonce'] != nonce:\n"
        "                raise JWTValidationException('Claim \"nonce\" is invalid.')"
    )
    new_nonce_block = (
        "            if nonce is not None:\n"
        "                if claims.get('nonce') != nonce:\n"
        "                    raise JWTValidationException('Claim \"nonce\" is missing or invalid.')\n"
        "            elif 'nonce' in claims:\n"
        "                raise JWTValidationException('Claim \"nonce\" is invalid.')"
    )
    assert jv_txt.count(old_nonce_block) == 2, "Expected 2 occurrences of nonce check in jwt_verifier.py"
    jv_file.write_text(jv_txt.replace(old_nonce_block, new_nonce_block), encoding="utf-8")

    ju_file = patched_dir / "jwt_utils.py"
    ju_txt = ju_file.read_text(encoding="utf-8")
    target_leeway = "issuer=None,\n                                   leeway=LEEWAY)"
    assert target_leeway in ju_txt
    ju_file.write_text(
        ju_txt.replace(target_leeway, "issuer=None,\n                                   leeway=leeway)"),
        encoding="utf-8",
    )

    # Syntax gate on patched Python files
    py_compile.compile(str(jv_file), doraise=True)
    py_compile.compile(str(ju_file), doraise=True)

    # Generate ephemeral 2048-bit RSA test keypair & JWK in memory
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(priv_key.public_key()))
    pub_jwk["kid"] = "unit-test-kid-01"
    pub_jwk["alg"] = "RS256"
    pub_jwk["use"] = "sig"

    issuer = "https://unit-test.okta.com/oauth2/default"
    client_id = "0oa_unit_test_client"
    now_ts = int(time.time())

    token_with_matching_nonce = jwt.encode(
        {"iss": issuer, "aud": client_id, "iat": now_ts - 5, "exp": now_ts + 300, "nonce": "expected-nonce-abc"},
        priv_key,
        algorithm="RS256",
        headers={"kid": "unit-test-kid-01"},
    )
    token_missing_nonce = jwt.encode(
        {"iss": issuer, "aud": client_id, "iat": now_ts - 5, "exp": now_ts + 300},
        priv_key,
        algorithm="RS256",
        headers={"kid": "unit-test-kid-01"},
    )
    token_expired_30s_ago = jwt.encode(
        {"iss": issuer, "aud": client_id, "iat": now_ts - 120, "exp": now_ts - 30},
        priv_key,
        algorithm="RS256",
        headers={"kid": "unit-test-kid-01"},
    )

    results: Dict[str, Any] = {}
    for label, pkg_path in (("unpatched", unpatched_dir), ("patched", patched_dir)):
        pkg_name = f"okta_verifier_{label}"
        load_package_from_dir(pkg_name, pkg_path)
        jv_mod = importlib.import_module(f"{pkg_name}.jwt_verifier")
        ju_mod = importlib.import_module(f"{pkg_name}.jwt_utils")
        exc_mod = importlib.import_module(f"{pkg_name}.exceptions")

        class OfflineRequestExecutor:
            def __init__(self, **kw):
                pass

            def clear_cache(self):
                pass

        verifier = jv_mod.BaseJWTVerifier(
            issuer=issuer,
            client_id=client_id,
            audience="api://default",
            request_executor=OfflineRequestExecutor,
        )
        verifier.get_jwk_sync = lambda kid: pub_jwk

        async def _async_jwk(kid):
            return pub_jwk

        verifier.get_jwk = _async_jwk

        # 1. Valid token WITH matching nonce
        try:
            verifier.verify_id_token_sync(token_with_matching_nonce, nonce="expected-nonce-abc")
            valid_nonce_accepted = True
        except exc_mod.JWTValidationException:
            valid_nonce_accepted = False

        # 2. Token MISSING 'nonce' claim when caller explicitly requires nonce='expected-nonce-abc'
        try:
            verifier.verify_id_token_sync(token_missing_nonce, nonce="expected-nonce-abc")
            missing_nonce_sync_accepted = True
        except exc_mod.JWTValidationException:
            missing_nonce_sync_accepted = False

        try:
            asyncio.run(verifier.verify_id_token(token_missing_nonce, nonce="expected-nonce-abc"))
            missing_nonce_async_accepted = True
        except exc_mod.JWTValidationException:
            missing_nonce_async_accepted = False

        # 3. Backward compatibility: token without nonce when caller passes nonce=None
        try:
            verifier.verify_id_token_sync(token_missing_nonce, nonce=None)
            no_expected_nonce_accepted = True
        except exc_mod.JWTValidationException:
            no_expected_nonce_accepted = False

        # 4. Expired 30s ago with explicit leeway=0
        try:
            ju_mod.JWTUtils.verify_expiration(token_expired_30s_ago, leeway=0)
            expired_30s_with_zero_leeway_accepted = True
        except exc_mod.JWTValidationException:
            expired_30s_with_zero_leeway_accepted = False

        results[label] = {
            "valid_token_with_matching_nonce_accepted": valid_nonce_accepted,
            "missing_nonce_claim_when_nonce_expected_sync_accepted": missing_nonce_sync_accepted,
            "missing_nonce_claim_when_nonce_expected_async_accepted": missing_nonce_async_accepted,
            "no_expected_nonce_backward_compat_accepted": no_expected_nonce_accepted,
            "expired_30s_token_with_leeway_0_accepted": expired_30s_with_zero_leeway_accepted,
        }
    return results


NODE_HARNESS_MJS = r"""
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const express = require('express');
const cors = require('cors');
const { RequestCookies, ResponseCookies } = require('@edge-runtime/cookies');

const SANDBOX_BASE = path.resolve(import.meta.dirname, '..');
const RUNTIME_DIR = import.meta.dirname;

function copyDirSync(src, dest) {
  fs.rmSync(dest, { recursive: true, force: true });
  fs.cpSync(src, dest, { recursive: true });
}

async function testZendeskZcli() {
  const zcliCorePkg = path.join(SANDBOX_BASE, 'repos', 'zendesk-zcli-core', 'package');
  const nmZendeskCore = path.join(RUNTIME_DIR, 'node_modules', '@zendesk', 'zcli-core');
  fs.mkdirSync(path.dirname(nmZendeskCore), { recursive: true });
  copyDirSync(zcliCorePkg, nmZendeskCore);
  // Safe no-op stubs for 'open' & '@oclif/plugin-plugins' so zcli-core never touches OS browser or keychain
  const nmOpen = path.join(RUNTIME_DIR, 'node_modules', 'open');
  fs.mkdirSync(nmOpen, { recursive: true });
  fs.writeFileSync(path.join(nmOpen, 'package.json'), JSON.stringify({ name: 'open', version: '8.4.2', main: 'index.js' }));
  fs.writeFileSync(path.join(nmOpen, 'index.js'), 'module.exports = async () => {};\n');
  const nmOclif = path.join(RUNTIME_DIR, 'node_modules', '@oclif', 'plugin-plugins', 'lib');
  fs.mkdirSync(nmOclif, { recursive: true });
  fs.writeFileSync(path.join(nmOclif, 'plugins.js'), 'module.exports = { default: class {} };\n');

  const zcliAppsSrc = path.join(SANDBOX_BASE, 'repos', 'zendesk-zcli-apps', 'package', 'dist');
  const zcliCoreSrc = path.join(SANDBOX_BASE, 'repos', 'zendesk-zcli-core', 'package', 'dist');

  const unpatchedApps = path.join(RUNTIME_DIR, 'zcli_unpatched_apps');
  const patchedApps = path.join(RUNTIME_DIR, 'zcli_patched_apps');
  const unpatchedCore = path.join(RUNTIME_DIR, 'zcli_unpatched_core');
  const patchedCore = path.join(RUNTIME_DIR, 'zcli_patched_core');

  copyDirSync(zcliAppsSrc, unpatchedApps);
  copyDirSync(zcliAppsSrc, patchedApps);
  copyDirSync(zcliCoreSrc, unpatchedCore);
  copyDirSync(zcliCoreSrc, patchedCore);

  // Patch getAppSettings.js to redact param.secure === true for /app.json & patch oauth.js listen('127.0.0.1')
  const patchedGetSettings = path.join(patchedApps, 'utils', 'getAppSettings.js');
  let gsTxt = fs.readFileSync(patchedGetSettings, 'utf8');
  gsTxt = gsTxt.replace(
    /if \(configContainsParam\(param\.name\)\) \{\r?\n\s+result\[param\.name\] = configParams\[param\.name\];\r?\n\s+\}/,
    'if (configContainsParam(param.name) && !param.secure) {\n            result[param.name] = configParams[param.name];\n        }'
  );
  fs.writeFileSync(patchedGetSettings, gsTxt, 'utf8');

  const patchedOauth = path.join(patchedCore, 'lib', 'oauth.js');
  let oaTxt = fs.readFileSync(patchedOauth, 'utf8');
  oaTxt = oaTxt.replace('server.listen(port, () => {', "server.listen(port, '127.0.0.1', () => {");
  fs.writeFileSync(patchedOauth, oaTxt, 'utf8');

  // Create dummy Zendesk app directory with a "secure": true parameter
  const dummyAppDir = path.join(RUNTIME_DIR, 'dummy_zendesk_app');
  fs.rmSync(dummyAppDir, { recursive: true, force: true });
  fs.mkdirSync(path.join(dummyAppDir, 'assets'), { recursive: true });
  fs.writeFileSync(
    path.join(dummyAppDir, 'manifest.json'),
    JSON.stringify({
      name: 'Dummy Enterprise Billing App',
      author: { name: 'Test', email: 'test@example.com' },
      defaultLocale: 'en',
      private: true,
      location: { support: { ticket_sidebar: 'assets/iframe.html' } },
      version: '1.0.0',
      frameworkVersion: '2.0',
      parameters: [
        { name: 'stripeSecretKey', type: 'text', secure: true },
        { name: 'publicRegion', type: 'text', secure: false }
      ]
    }, null, 2)
  );
  fs.writeFileSync(
    path.join(dummyAppDir, 'zcli.apps.config.json'),
    JSON.stringify({
      app_id: 42,
      parameters: {
        stripeSecretKey: 'DUMMY_UNIT_TEST_SECRET_KEY_999',
        publicRegion: 'us-east-1'
      }
    }, null, 2)
  );

  async function runServerVariant(appsDir, isPatched) {
    const { buildAppJSON } = require(path.join(appsDir, 'lib', 'buildAppJSON.js'));
    const app = express();
    if (isPatched) {
      app.use(cors({ origin: /^https:\/\/([a-z0-9-]+\.)?(zendesk|zd-dev|zd-staging)\.com$/i }));
    } else {
      // Exact line from zendesk/zcli packages/zcli-apps/src/commands/apps/server.ts:49
      app.use(cors());
    }
    const server = await new Promise((resolve) => {
      const s = app.listen(0, '127.0.0.1', () => resolve(s));
    });
    const port = server.address().port;
    const appJSON = await buildAppJSON([dummyAppDir], port);
    app.get('/app.json', (req, res) => {
      res.setHeader('Content-Type', 'application/json');
      res.end(JSON.stringify(appJSON));
    });

    try {
      const evilRes = await fetch(`http://127.0.0.1:${port}/app.json`, {
        headers: { Origin: 'https://untrusted-site.example' }
      });
      const evilCors = evilRes.headers.get('access-control-allow-origin');
      const bodyText = await evilRes.text();

      const zdRes = await fetch(`http://127.0.0.1:${port}/app.json`, {
        headers: { Origin: 'https://acme.zendesk.com' }
      });
      const zdCors = zdRes.headers.get('access-control-allow-origin');

      return {
        untrusted_origin_acao_header: evilCors,
        zendesk_origin_acao_header: zdCors,
        secure_secret_present_in_app_json: bodyText.includes('DUMMY_UNIT_TEST_SECRET_KEY_999'),
        public_param_present_in_app_json: bodyText.includes('us-east-1')
      };
    } finally {
      server.closeAllConnections?.();
      server.close();
    }
  }

  // Also verify startCallbackServer runtime socket bind host (undefined => 0.0.0.0/:: vs '127.0.0.1')
  async function checkOauthBind(coreDir) {
    const oauthMod = require(path.join(coreDir, 'lib', 'oauth.js'));
    const probe = http.createServer();
    await new Promise((r) => probe.listen(0, '127.0.0.1', r));
    const freePort = probe.address().port;
    await new Promise((r) => probe.close(r));

    let observedHostArg = null;
    const origListen = http.Server.prototype.listen;
    http.Server.prototype.listen = function (...args) {
      observedHostArg = typeof args[1] === 'string' ? args[1] : 'ALL_INTERFACES_0.0.0.0_OR_::';
      return origListen.apply(this, args);
    };
    let started;
    try {
      started = await oauthMod.startCallbackServer('test-state-123', [freePort]);
      return {
        runtime_listen_host_arg: observedHostArg,
        listening_port: started.port
      };
    } finally {
      http.Server.prototype.listen = origListen;
      if (started) started.close();
    }
  }

  return {
    unpatched: {
      apps_server: await runServerVariant(unpatchedApps, false),
      oauth_callback: await checkOauthBind(unpatchedCore)
    },
    patched: {
      apps_server: await runServerVariant(patchedApps, true),
      oauth_callback: await checkOauthBind(patchedCore)
    }
  };
}

async function testAuth0Nextjs() {
  const auth0Dist = path.join(SANDBOX_BASE, 'auth0_repos', 'auth0-nextjs-auth0', 'package', 'dist');
  const unpatchedDir = path.join(RUNTIME_DIR, 'auth0_unpatched_dist');
  const patchedDir = path.join(RUNTIME_DIR, 'auth0_patched_dist');

  copyDirSync(auth0Dist, unpatchedDir);
  copyDirSync(auth0Dist, patchedDir);

  // 1. Patch fetcher.js in patchedDir
  const pFetcher = path.join(patchedDir, 'server', 'fetcher.js');
  let fTxt = fs.readFileSync(pFetcher, 'utf8');
  fTxt = fTxt.replace(
    /\/\/ Create Request object with resolved URL\r?\n\s+return new Request\(resolvedUrl, init\);/,
    'if (this.config.baseUrl) {\n            const expectedOrigin = new URL(this.config.baseUrl).origin;\n            if (new URL(resolvedUrl.toString()).origin !== expectedOrigin) {\n                throw new TypeError("Refusing to send authenticated request outside configured baseUrl origin.");\n            }\n        }\n        return new Request(resolvedUrl, init);'
  );
  fs.writeFileSync(pFetcher, fTxt, 'utf8');

  // 2. Patch proxy.js in patchedDir
  const pProxy = path.join(patchedDir, 'utils', 'proxy.js');
  let prTxt = fs.readFileSync(pProxy, 'utf8');
  prTxt = prTxt.replace(
    'if (remainingPath && remainingPath !== "/") {',
    'if (remainingPath && remainingPath !== "/") {\n        const decodedRemaining = decodeURIComponent(remainingPath);\n        if (decodedRemaining.split("/").some((s) => s === "." || s === "..")) {\n            throw new Error("Refusing to proxy path containing dot-segments.");\n        }'
  );
  fs.writeFileSync(pProxy, prTxt, 'utf8');

  // 3. Patch cookies.js in patchedDir (sparse O(MAX_CHUNKS + presentCookies) cleanup)
  const pCookies = path.join(patchedDir, 'server', 'cookies.js');
  let cTxt = fs.readFileSync(pCookies, 'utf8');
  cTxt = cTxt.replace(
    /const clearUpTo = getClearUpTo\(reqCookies, name\);\r?\n\s+for \(let i = 0; i < clearUpTo; i\+\+\) \{\r?\n\s+deleteCookie\(resCookies, `\$\{name\}\$\{CHUNK_PREFIX\}\$\{i\}`, options\);\r?\n\s+\}/,
    'for (let i = 0; i < MAX_CHUNKS; i++) {\n        deleteCookie(resCookies, `${name}${CHUNK_PREFIX}${i}`, options);\n    }\n    for (const cookie of getAllChunkedCookies(reqCookies, name)) {\n        deleteCookie(resCookies, cookie.name, options);\n    }'
  );
  fs.writeFileSync(pCookies, cTxt, 'utf8');

  async function runAuth0Variant(distDir) {
    const fetcherMod = await import(pathToFileURL(path.join(distDir, 'server', 'fetcher.js')).href + `?t=${Date.now()}`);
    const proxyMod = await import(pathToFileURL(path.join(distDir, 'utils', 'proxy.js')).href + `?t=${Date.now()}`);
    const cookiesMod = await import(pathToFileURL(path.join(distDir, 'server', 'cookies.js')).href + `?t=${Date.now()}`);

    // Test A: Fetcher.fetchWithAuth with baseUrl configured + offline mock fetch
    const capturedCalls = [];
    const fetcher = new fetcherMod.Fetcher(
      {
        baseUrl: 'https://api.trusted.internal',
        httpOptions: () => ({}),
        getAccessToken: async () => 'DUMMY_UNIT_TEST_BEARER_TOKEN',
        fetch: async (url, options) => {
          const h = new Headers(options?.headers);
          capturedCalls.push({
            sent_to_origin: new URL(url.toString()).origin,
            has_bearer_token: (h.get('authorization') || '').includes('DUMMY_UNIT_TEST_BEARER_TOKEN')
          });
          return new Response(JSON.stringify({ ok: true }), { status: 200 });
        }
      },
      {}
    );

    await fetcher.fetchWithAuth('/v1/orders');
    let crossOriginForwarded = false;
    let crossOriginBearerAttached = false;
    try {
      await fetcher.fetchWithAuth('https://untrusted-collector.example/endpoint');
      const last = capturedCalls[capturedCalls.length - 1];
      crossOriginForwarded = last.sent_to_origin === 'https://untrusted-collector.example';
      crossOriginBearerAttached = last.has_bearer_token;
    } catch {
      crossOriginForwarded = false;
      crossOriginBearerAttached = false;
    }

    // Test B1: transformTargetUrl direct NextRequest mock with raw %2e%2e pathname
    const validTarget = proxyMod.transformTargetUrl(
      { nextUrl: { pathname: '/me/v1/profile', searchParams: new URLSearchParams() } },
      { proxyPath: '/me', targetBaseUrl: 'https://tenant.example.test/me/v1' }
    );

    let rawPercent2eEscapedPathname = null;
    try {
      const escaped = proxyMod.transformTargetUrl(
        { nextUrl: { pathname: '/me/%2e%2e/%2e%2e/other-api', searchParams: new URLSearchParams() } },
        { proxyPath: '/me', targetBaseUrl: 'https://tenant.example.test/me/v1' }
      );
      rawPercent2eEscapedPathname = escaped.pathname;
    } catch (e) {
      rawPercent2eEscapedPathname = `BLOCKED: ${e.message}`;
    }

    // Test B2: Standard WHATWG URL pre-normalization check (simulating standard Fetch/Edge Request URL)
    const whatwgParsed = new URL('https://myapp.example.test/me/%2e%2e/%2e%2e/other-api');
    const reachesMeHandlerAfterWhatwgUrlParse = whatwgParsed.pathname.startsWith('/me/');

    // Test C: deleteChunkedCookie with a single sparse cookie `__session__50=dummy`
    const reqHeaders = new Headers({ cookie: '__session__50=dummy' });
    const reqCookies = new RequestCookies(reqHeaders);
    const resHeaders = new Headers();
    const resCookies = new ResponseCookies(resHeaders);
    cookiesMod.deleteChunkedCookie('__session', reqCookies, resCookies, false, { path: '/' });
    const setCookieCount = resCookies.getAll().length;

    return {
      fetcher_valid_relative_call_ok: capturedCalls[0]?.sent_to_origin === 'https://api.trusted.internal',
      fetcher_cross_origin_token_leaked: crossOriginForwarded && crossOriginBearerAttached,
      proxy_valid_me_path: validTarget.pathname,
      proxy_raw_percent2e_result: rawPercent2eEscapedPathname,
      proxy_reaches_me_handler_after_standard_whatwg_url_parse: reachesMeHandlerAfterWhatwgUrlParse,
      cookies_set_cookie_headers_emitted_for_single_idx_50: setCookieCount
    };
  }

  return {
    unpatched: await runAuth0Variant(unpatchedDir),
    patched: await runAuth0Variant(patchedDir)
  };
}

const zendesk = await testZendeskZcli();
const auth0 = await testAuth0Nextjs();
console.log(JSON.stringify({ zendesk, auth0 }, null, 2));
process.exit(0);
"""


def main() -> int:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    print("[v18] [1/2] Running Offline Python Runtime Verification (okta-jwt-verifier-python v0.5.0)...")
    okta_results = verify_okta_python_runtime()
    print("      -> Okta Python runtime results:", json.dumps(okta_results))

    node_script = RUNTIME_DIR / "verify_node_packages.mjs"
    node_script.write_text(NODE_HARNESS_MJS, encoding="utf-8")

    # Validate JS syntax (`node --check`) before running
    chk = subprocess.run(["node", "--check", str(node_script)], capture_output=True, text=True, timeout=15)
    if chk.returncode != 0:
        raise RuntimeError(f"node --check failed:\n{chk.stderr}")

    print("[v18] [2/2] Running Offline Node.js Runtime Verification (zendesk/zcli & @auth0/nextjs-auth0)...")
    proc = subprocess.run(
        ["node", str(node_script)],
        cwd=str(RUNTIME_DIR),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Node runtime verification failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")

    node_results = json.loads(proc.stdout)
    report = {
        "version": "v18",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "external_network_calls": 0,
        "okta_jwt_verifier_python_0_5_0": okta_results,
        "zendesk_zcli_2_1_0": node_results["zendesk"],
        "auth0_nextjs_auth0_4_33_0": node_results["auth0"],
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[v18] Saved full runtime verification report to {OUT_JSON}")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
