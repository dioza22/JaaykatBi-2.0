"""Retry-with-backoff behavior for outbound WhatsApp API calls — added after
observing transient 400/403 failures against Meta's sandbox test number
under rapid testing that cleared on manual retry seconds later."""

import httpx
import pytest

from app.services.whatsapp import client as client_module
from app.services.whatsapp.client import WhatsAppClient

pytestmark = pytest.mark.asyncio


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Returns one canned status per call, in order; repeats the last one
    once the script is exhausted."""

    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        index = min(self.call_count, len(self.statuses) - 1)
        status = self.statuses[index]
        self.call_count += 1
        body = {"error": {"message": "rate limited"}} if status >= 400 else {"messages": [{"id": "wamid.TEST"}]}
        return httpx.Response(status, json=body, request=request)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    async def _instant_sleep(_seconds):
        return None

    monkeypatch.setattr(client_module.asyncio, "sleep", _instant_sleep)


@pytest.fixture
def _patched_httpx_client(monkeypatch):
    def _install(statuses: list[int]) -> _ScriptedTransport:
        transport = _ScriptedTransport(statuses)
        original_init = httpx.AsyncClient.__init__

        def _patched_init(self, *args, **kwargs):
            kwargs["transport"] = transport
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_init)
        return transport

    return _install


async def test_post_succeeds_immediately_on_200(_patched_httpx_client):
    transport = _patched_httpx_client([200])
    result = await WhatsAppClient()._post({"messaging_product": "whatsapp"})
    assert result == {"messages": [{"id": "wamid.TEST"}]}
    assert transport.call_count == 1


async def test_post_retries_transient_failure_then_succeeds(_patched_httpx_client):
    transport = _patched_httpx_client([429, 200])
    result = await WhatsAppClient()._post({"messaging_product": "whatsapp"})
    assert result == {"messages": [{"id": "wamid.TEST"}]}
    assert transport.call_count == 2


async def test_post_retries_the_400_seen_in_production_then_succeeds(_patched_httpx_client):
    transport = _patched_httpx_client([400, 400, 200])
    result = await WhatsAppClient()._post({"messaging_product": "whatsapp"})
    assert result == {"messages": [{"id": "wamid.TEST"}]}
    assert transport.call_count == 3


async def test_post_gives_up_after_all_attempts_on_persistent_failure(_patched_httpx_client):
    transport = _patched_httpx_client([500, 500, 500])
    with pytest.raises(httpx.HTTPStatusError):
        await WhatsAppClient()._post({"messaging_product": "whatsapp"})
    assert transport.call_count == 3
