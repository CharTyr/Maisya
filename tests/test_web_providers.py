import json
import logging
import sys
from http.client import HTTPMessage
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from maibot_sdk.context import PluginContext, PluginPaths
from test_foundation import loaded

from maisya.config import Settings
from maisya.scout import Scout
from maisya.services import Services
from maisya.web import GOOGLE_SEARCH_PLUGIN_ID, WebRouter, _SafeRedirectHandler


def _third_party_config(
    *,
    enabled=True,
    default_engine="exa",
    exa_keys=None,
    tavily_keys=None,
    tavily_key="",
    tavily_base_url="",
    anysearch_enabled=True,
    anysearch_keys=None,
    you_enabled=False,
    you_news_enabled=False,
    you_contents_enabled=False,
    you_keys=None,
    deepseek_enabled=False,
    deepseek_keys=None,
    zhihu_cookies="",
    proxy="",
):
    return {
        "plugin": {"name": "google_search", "version": "4.1.1", "enabled": enabled},
        "search_backend": {
            "default_engine": default_engine,
            "max_results": 5,
            "timeout": 10,
            "proxy": proxy,
            "content_timeout": 10,
            "max_content_length": 3000,
            "zhihu_cookies": zhihu_cookies,
        },
        "engines": {
            "exa_enabled": exa_keys is not None,
            "exa_api_keys": list(exa_keys or []),
            "tavily_enabled": bool(tavily_keys or tavily_key),
            "tavily_api_keys": list(tavily_keys or []),
            "tavily_api_key": tavily_key,
            "tavily_base_url": tavily_base_url,
            "anysearch_enabled": anysearch_enabled,
            "anysearch_api_keys": list(anysearch_keys or []),
            "you_enabled": you_enabled,
            "you_news_enabled": you_news_enabled,
            "you_contents_enabled": you_contents_enabled,
            "you_api_keys": list(you_keys or []),
            "deepseek_enabled": deepseek_enabled,
            "deepseek_api_keys": list(deepseek_keys or []),
            "duckduckgo_enabled": False,
        },
    }


@pytest.mark.asyncio
async def test_auto_prefers_host_first_and_only_falls_back_when_disabled():
    calls = []

    class Host:
        def __init__(self, behavior):
            self.behavior = behavior

        async def call_capability(self, name, **kwargs):
            calls.append((name, kwargs))
            if name in {"web.search", "web.fetch"}:
                if self.behavior == "ok":
                    return {
                        "success": True,
                        "result": {
                            "query": kwargs.get("query", ""),
                            "results": [
                                {
                                    "title": "Host Result",
                                    "url": "https://host.example/a",
                                    "snippet": "from host",
                                }
                            ],
                            "content_kind": "search_results",
                            "service": "host:exa",
                        },
                    }
                if self.behavior == "disabled_error":
                    return {"success": False, "error": "统一联网功能未启用"}
                if self.behavior == "disabled_exception":
                    raise RuntimeError("当前操作未配置联网服务")
                return {"success": False, "error": "HTTP 502 upstream timeout"}
            if name == "config.get_plugin":
                assert kwargs["plugin_name"] == GOOGLE_SEARCH_PLUGIN_ID
                return _third_party_config(exa_keys=["test-exa-key-1"])
            raise AssertionError(name)

    # 1. Host enabled: calls host first and never calls config.get_plugin.
    http_calls = []

    def stub_http(method, url, **kwargs):
        http_calls.append((method, url, kwargs))
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "results": [
                        {
                            "title": "Exa Hit",
                            "url": "https://exa.example/doc",
                            "highlights": ["excerpt"],
                            "publishedDate": "2026-10-01",
                        }
                    ]
                }
            ).encode(),
            url,
        )

    router_ok = WebRouter(Host("ok"), http_request=stub_http)
    res_ok = await router_ok.search("Maisya", 3, {"chat_id": "c1", "owner": "chat"})
    assert res_ok["service"] == "host:exa"
    assert [c[0] for c in calls] == ["web.search"]
    assert http_calls == []

    # 2. Host enabled + ordinary upstream error: raises directly, NO fallback.
    calls.clear()
    router_err = WebRouter(Host("upstream_error"), http_request=stub_http)
    with pytest.raises(RuntimeError, match="HTTP 502"):
        await router_err.search("Maisya", 3, {"chat_id": "c1", "owner": "chat"})
    assert [c[0] for c in calls] == ["web.search"]
    assert http_calls == []

    # 3. Host disabled via dict error or raised exception: falls back to google-search-plugin.
    for mode in ("disabled_error", "disabled_exception"):
        calls.clear()
        http_calls.clear()
        router_fb = WebRouter(Host(mode), http_request=stub_http)
        res_fb = await router_fb.search("Maisya", 3, {"chat_id": "c1", "owner": "chat"})
        assert res_fb["content_kind"] == "search_results"
        assert res_fb["service"] == "google_search_plugin:exa"
        assert res_fb["results"][0]["url"] == "https://exa.example/doc"
        assert res_fb["results"][0]["published_at"] == "2026-10-01"
        assert [c[0] for c in calls] == ["web.search", "config.get_plugin"]


@pytest.mark.asyncio
async def test_config_get_plugin_handles_real_sdk_unwrap_shapes():
    plugin_cfg = _third_party_config(exa_keys=["sdk-exa-key"])

    def stub_http(method, url, **kwargs):
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {"results": [{"title": "SDK Unwrapped", "url": "https://sdk.example/1", "highlights": ["ok"]}]}
            ).encode(),
            url,
        )

    # Real PluginContext unwraps config.get_plugin's "value" key: dict on hit, None on miss.
    async def rpc_hit(method, plugin_id, payload, **kwargs):
        cap = payload["capability"]
        if cap == "web.search":
            return {"success": False, "error": "统一联网功能未启用"}
        if cap == "config.get_plugin":
            return {"success": True, "value": plugin_cfg}
        raise AssertionError(cap)

    router_hit = WebRouter(
        PluginContext("chartyr.maisya", rpc_call=rpc_hit), http_request=stub_http
    )
    res = await router_hit.search("test", 2, {"chat_id": "c", "owner": "chat"})
    assert res["results"][0]["title"] == "SDK Unwrapped"

    async def rpc_miss(method, plugin_id, payload, **kwargs):
        cap = payload["capability"]
        if cap == "web.search":
            return {"success": False, "error": "统一联网功能未启用"}
        if cap == "config.get_plugin":
            return {"success": False, "value": None, "error": "插件未加载"}
        raise AssertionError(cap)

    router_miss = WebRouter(
        PluginContext("chartyr.maisya", rpc_call=rpc_miss), http_request=stub_http
    )
    with pytest.raises(RuntimeError, match="未检测到可用"):
        await router_miss.search("test", 2, {"chat_id": "c", "owner": "chat"})


@pytest.mark.asyncio
async def test_google_search_plugin_engine_fallback_chain_and_secret_redaction():
    exa_secret = "exa-top-secret-token-9988"
    tavily_secret = "tvly-top-secret-token-7766"
    deepseek_secret = "sk-ds-top-secret-5544"
    seen = []

    cfg = _third_party_config(
        default_engine="exa",
        exa_keys=[exa_secret],
        tavily_keys=[tavily_secret],
        tavily_base_url="https://relay.example.com",
        anysearch_enabled=True,
        anysearch_keys=[],
        you_enabled=True,
        you_keys=["you-secret-1122"],
        deepseek_enabled=True,
        deepseek_keys=[deepseek_secret],
        zhihu_cookies="z_c0=cookie-secret-value",
    )

    class StubHost:
        async def call_capability(self, name, **kwargs):
            if name == "config.get_plugin":
                return cfg
            raise AssertionError(name)

    def stub_chain(method, url, *, headers=None, json_body=None, **kwargs):
        seen.append((method, url, headers or {}, json_body))
        if url.startswith("https://api.exa.ai"):
            return (500, {"content-type": "application/json"}, b"{}", url)
        if url.startswith("https://api.deepseek.com"):
            # Even if upstream raises or fails, never stops fallback; also test redaction below.
            return (401, {"content-type": "application/json"}, b"bad key", url)
        if url.startswith("https://relay.example.com/search"):
            assert headers.get("Authorization") == f"Bearer {tavily_secret}"
            assert "api_key" not in (json_body or {})
            return (
                200,
                {"content-type": "application/json"},
                json.dumps(
                    {
                        "results": [
                            {
                                "title": f"Result echoing {tavily_secret}",
                                "url": "https://tavily.example/hit",
                                "content": f"snippet with {exa_secret} and z_c0=cookie-secret-value",
                            }
                        ]
                    }
                ).encode(),
                url,
            )
        raise AssertionError(f"Unexpected call {url}")

    router = WebRouter(StubHost(), provider="google_search_plugin", http_request=stub_chain)
    result = await router.search("AI news", 4, {"chat_id": "c", "owner": "chat"})
    # default_engine="exa" runs first, then deepseek, then tavily succeeds.
    assert [u.split("/")[2] for _, u, _, _ in seen] == [
        "api.exa.ai",
        "api.deepseek.com",
        "relay.example.com",
    ]
    assert result["service"] == "google_search_plugin:tavily"
    serialized = json.dumps(result, ensure_ascii=False)
    for secret in (exa_secret, tavily_secret, deepseek_secret, "cookie-secret-value"):
        assert secret not in serialized
    assert "[REDACTED]" in result["results"][0]["title"]
    assert not hasattr(router, "_third_party_config")

    # Verify anysearch anonymous request and you / deepseek formats work, plus error scrubbing.
    cfg_any = _third_party_config(default_engine="anysearch", anysearch_enabled=True)

    class HostAny:
        async def call_capability(self, name, **kwargs):
            return cfg_any

    def stub_any(method, url, *, headers=None, json_body=None, **kwargs):
        assert url == "https://api.anysearch.com/v1/search"
        assert "Authorization" not in (headers or {})
        return (
            200,
            {"content-type": "application/json"},
            json.dumps(
                {
                    "code": 0,
                    "data": {
                        "results": [
                            {"title": "Any Hit", "url": "https://any.example/1", "snippet": "fast"}
                        ]
                    },
                }
            ).encode(),
            url,
        )

    res_any = await WebRouter(
        HostAny(), provider="google_search_plugin", http_request=stub_any
    ).search("q", 3, {"chat_id": "c", "owner": "chat"})
    assert res_any["service"] == "google_search_plugin:anysearch"
    assert res_any["results"][0]["url"] == "https://any.example/1"

    # Exception echoing an API key must be scrubbed before leaving WebRouter.
    def stub_boom(method, url, **kwargs):
        raise RuntimeError(f"Connection failed with key {exa_secret}")

    router_boom = WebRouter(StubHost(), provider="google_search_plugin", http_request=stub_boom)
    with pytest.raises(RuntimeError) as exc_info:
        await router_boom.search("q", 2, {"chat_id": "c", "owner": "chat"})
    assert exa_secret not in str(exc_info.value)


@pytest.mark.asyncio
async def test_web_fetch_fallback_html_you_contents_and_ssrf_redirect_block():
    you_secret = "you-contents-key-3344"

    class HostWeb:
        def __init__(self, cfg):
            self.cfg = cfg
            self.calls = []

        async def call_capability(self, name, **kwargs):
            self.calls.append((name, kwargs))
            if name == "web.fetch":
                return {"success": False, "error": "统一联网功能未启用"}
            if name == "config.get_plugin":
                return self.cfg
            raise AssertionError(name)

    # 1. HTML extraction strips script/style/nav.
    html_body = b"""
    <html><head><title>Test Page</title><style>.hidden{display:none}</style></head>
    <body>
      <nav>Site Menu Links</nav>
      <script>window.secret = "should_not_appear";</script>
      <main><h1>Main Heading</h1><p>Useful paragraph about Maisya search.</p></main>
    </body></html>
    """
    router = WebRouter(
        HostWeb(_third_party_config(exa_keys=["k1"])),
        http_request=lambda m, u, **kw: (
            200,
            {"content-type": "text/html; charset=utf-8"},
            html_body,
            u,
        ),
    )
    fetched = await router.fetch("https://docs.example.org/page", {"chat_id": "c", "owner": "chat"})
    assert fetched["content_kind"] == "page_text"
    assert fetched["title"] == "Test Page"
    assert "Useful paragraph about Maisya search." in fetched["content"]
    assert "should_not_appear" not in fetched["content"]
    assert "Site Menu Links" not in fetched["content"]

    # 2. you_contents priority when enabled with keys.
    you_host = HostWeb(_third_party_config(you_contents_enabled=True, you_keys=[you_secret]))
    you_router = WebRouter(
        you_host,
        http_request=lambda m, u, **kw: (
            200,
            {"content-type": "application/json"},
            json.dumps([{"url": "https://docs.example.org/p2", "markdown": f"# Title\nBody {you_secret}"}]).encode(),
            u,
        ),
    )
    fetched_you = await you_router.fetch("https://docs.example.org/p2", {"chat_id": "c", "owner": "chat"})
    assert fetched_you["service"] == "google_search_plugin:you_contents"
    assert you_secret not in fetched_you["content"] and "[REDACTED]" in fetched_you["content"]

    # 3. Direct SSRF block before RPC / HTTP.
    host_ssrf = HostWeb(_third_party_config(exa_keys=["k1"]))
    scout = Scout(host_ssrf)
    for blocked in ("http://127.0.0.1/secret", "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(ValueError):
            await scout.call("web_fetch", {"url": blocked}, {"chat_id": "c", "owner": "chat"})
    assert host_ssrf.calls == []

    # 4. _SafeRedirectHandler must reject 302 redirects to loopback/metadata IPs.
    import urllib.request

    handler = _SafeRedirectHandler()
    req = urllib.request.Request("https://public.example.com/start")
    for redirect_target in (
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",
        "/redirect-ok",
    ):
        headers = HTTPMessage()
        headers["Location"] = redirect_target
        if redirect_target.startswith("http://127") or redirect_target.startswith("http://169"):
            with pytest.raises(ValueError):
                handler.redirect_request(req, None, 302, "Found", headers, redirect_target)
        else:
            with patch("maisya.web._assert_public_host"):
                nxt = handler.redirect_request(req, None, 302, "Found", headers, redirect_target)
                assert nxt.full_url == "https://public.example.com/redirect-ok"


@pytest.mark.asyncio
async def test_services_web_bindings_and_explicit_modes(tmp_path):
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        server = tmp_path / "web_mcp.py"
        server.write_text(
            """import json, sys
for line in sys.stdin:
 request = json.loads(line)
 if 'id' not in request: continue
 method = request['method']
 if method == 'initialize': result = {'protocolVersion':'2025-11-25','capabilities':{'tools':{}}}
 elif method == 'tools/list': result = {'tools':[{'name':'web_get','inputSchema':{'type':'object','properties':{'url':{'type':'string'}},'required':['url'],'additionalProperties':False}}]}
 elif method == 'tools/call':
  result = {'content':[{'type':'text','text':json.dumps({'url':request['params']['arguments']['url'],'title':'MCP Page','content':'MCP fetched body'})}]}
 else: result = {}
 print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
""",
            encoding="utf-8",
        )
        services_state = await plugin.manage_read("services")
        # Reject writable MCP binding in services.web.
        bad_cfg = {
            "servers": {
                "rw_web": {
                    "command": [sys.executable, str(server)],
                    "tools": ["web_get"],
                    "readonly": False,
                }
            },
            "templates": {},
            "web": {"fetch": {"service": "rw_web", "tool": "web_get"}},
        }
        rejected = await plugin.manage_write(
            "services",
            json.dumps({"configuration": bad_cfg, "expected_version": services_state["version"]}),
        )
        assert not rejected["success"]

        # Accept readonly MCP + readonly API (admins-gated) bindings.
        good_cfg = {
            "servers": {
                "ro_mcp": {
                    "command": [sys.executable, str(server)],
                    "tools": ["web_get"],
                    "readonly": True,
                }
            },
            "apis": {
                "admin_search": {
                    "api_name": "third.search",
                    "readonly": True,
                    "access": "admins",
                    "schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                }
            },
            "web": {
                "search": {"api": "admin_search"},
                "fetch": {"service": "ro_mcp", "tool": "web_get"},
            },
        }
        saved = await plugin.manage_write(
            "services",
            json.dumps({"configuration": good_cfg, "expected_version": services_state["version"]}),
        )
        assert saved["success"], saved

        # Call plugin.runtime.services directly for mcp fetch.
        fetched = await plugin.runtime.services.call_web(
            "fetch",
            {"url": "https://example.org/mcp"},
            {"chat_id": "chat", "thread_id": "t1", "platform": "qq", "owner": "chat"},
            plugin.runtime,
            mode="mcp",
        )
        assert fetched["title"] == "MCP Page"
        assert "MCP fetched body" in fetched["content"]

        # Unauthenticated / self thread calling access="admins" plugin_api web search must be denied.
        with pytest.raises(PermissionError):
            await plugin.runtime.services.call_web(
                "search",
                {"query": "q", "max_results": 3},
                {"chat_id": "chat", "thread_id": "t1", "platform": "qq", "owner": "self"},
                plugin.runtime,
                mode="plugin_api",
            )

        # Authenticated admin succeeds via api.call and additionalProperties schema strips max_results cleanly.
        api_calls = []

        async def mock_cap(name, **kwargs):
            api_calls.append((name, kwargs))
            if name == "api.call":
                return {
                    "success": True,
                    "result": {
                        "query": kwargs["args"]["query"],
                        "results": [{"title": "API Hit", "url": "https://api.example/1", "snippet": "ok"}],
                    },
                }
            raise AssertionError(name)

        plugin.runtime.host.call_capability = mock_cap
        plugin.runtime.authorized = lambda ctx: ctx.get("grant", {}).get("source_msg_id") == "admin-msg"
        api_res = await plugin.runtime.services.call_web(
            "search",
            {"query": "hello", "max_results": 5},
            {
                "chat_id": "chat",
                "thread_id": "t1",
                "platform": "qq",
                "owner": "chat",
                "grant": {"source_msg_id": "admin-msg"},
            },
            plugin.runtime,
            mode="plugin_api",
        )
        assert api_res["results"][0]["title"] == "API Hit"
        assert api_calls[0][0] == "api.call"
        assert api_calls[0][1]["args"] == {"query": "hello"}
    finally:
        await plugin.on_unload()


@pytest.mark.asyncio
async def test_settings_validation_and_management_health_web_status(tmp_path):
    assert Settings().web_provider == "auto"
    assert Settings(web_provider="google_search_plugin").web_provider == "google_search_plugin"
    with pytest.raises(ValueError):
        Settings(web_provider="unknown_provider")
    assert Settings().ui_style == "millennium"
    assert Settings(ui_style="maiwork").ui_style == "maiwork"
    assert Settings(ui_style="auto").ui_style == "auto"
    with pytest.raises(ValueError):
        Settings(ui_style="neon_invalid")

    # Bare SimpleNamespace without call_capability (like loaded() fixture) must still return health safely.
    plugin = await loaded(tmp_path, ["qq:Alice"])
    try:
        health = await plugin.manage_read("health")
        assert health["success"]
        assert health["web"]["provider"] == "auto"
        assert health["web"]["third_party_enabled"] is False

        # When host exposes config.get_plugin with keys, health exposes engine names ONLY, never API keys.
        secret_key = "exa-health-never-leak-5566"

        async def call_cap(name, **kwargs):
            if name == "config.get_plugin":
                return _third_party_config(default_engine="exa", exa_keys=[secret_key])
            raise AssertionError(name)

        plugin._ctx.call_capability = call_cap
        health2 = await plugin.manage_read("health")
        assert health2["success"]
        assert health2["web"]["third_party_enabled"] is True
        assert health2["web"]["third_party_default"] == "exa"
        assert "exa" in health2["web"]["third_party_engines"]
        assert secret_key not in json.dumps(health2, ensure_ascii=False)
    finally:
        await plugin.on_unload()
