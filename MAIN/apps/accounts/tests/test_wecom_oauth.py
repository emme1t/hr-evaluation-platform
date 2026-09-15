import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from threading import Barrier, Lock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from django.conf import settings
from django.db import close_old_connections, connection
from django.db.models.query import QuerySet
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.roster.models import Employee, EmployeeCategory


class FakeWeComClient:
    def __init__(self, user_id="wx_e101", error=None):
        self.user_id = user_id
        self.error = error
        self.codes = []

    def authorization_url(self, state, redirect_uri):
        return f"https://wecom.example.test/oauth?state={state}&redirect_uri={redirect_uri}"

    def user_id_from_code(self, code):
        self.codes.append(code)
        if self.error:
            raise self.error
        return self.user_id


def set_oauth_session(
    client, state="valid-state", next_path="/tasks/", expires_at=None
):
    from apps.accounts.models import WeComOAuthState

    session = client.session
    session["wecom_oauth_next"] = next_path
    session.save()
    WeComOAuthState.objects.create(
        state_digest=sha256(state.encode()).hexdigest(),
        session_key=session.session_key,
        expires_at=expires_at or timezone.now() + timedelta(minutes=10),
    )


@pytest.mark.django_db
def test_start_generates_state_and_preserves_safe_same_site_next(client, monkeypatch):
    from apps.accounts.models import WeComOAuthState

    monkeypatch.setattr("apps.accounts.views.secrets.token_urlsafe", lambda _: "random-state")
    monkeypatch.setattr(
        "apps.accounts.views.get_wecom_client", lambda: FakeWeComClient()
    )

    response = client.get(reverse("accounts:wecom_start"), {"next": "/tasks/?page=2"})

    assert response.status_code == 302
    assert response.url.startswith("https://wecom.example.test/oauth?")
    assert "wecom_oauth_state" not in client.session
    assert client.session["wecom_oauth_next"] == "/tasks/?page=2"
    stored_state = WeComOAuthState.objects.get()
    assert stored_state.state_digest == sha256(b"random-state").hexdigest()
    assert stored_state.session_key == client.session.session_key
    assert stored_state.expires_at > timezone.now()


@pytest.mark.django_db
def test_offline_start_denies_locally_without_creating_oauth_state_or_session(
    client, settings
):
    from apps.accounts.models import WeComOAuthState

    settings.APP_ENV = "offline"

    response = client.get(reverse("accounts:wecom_start"), {"next": "/tasks/"})

    assert response.status_code == 403
    assert not response.has_header("Location")
    assert "登录失败，请联系管理员。" in response.content.decode()
    assert WeComOAuthState.objects.count() == 0
    assert response.wsgi_request.session.session_key is None
    assert not response.wsgi_request.session.modified


@pytest.mark.parametrize("next_value", ["https://attacker.example/", "//attacker.example/"])
@pytest.mark.django_db
def test_start_rejects_external_next_url(client, monkeypatch, next_value):
    monkeypatch.setattr("apps.accounts.views.secrets.token_urlsafe", lambda _: "random-state")
    monkeypatch.setattr(
        "apps.accounts.views.get_wecom_client", lambda: FakeWeComClient()
    )

    response = client.get(reverse("accounts:wecom_start"), {"next": next_value})

    assert response.status_code == 302
    assert client.session["wecom_oauth_next"] == "/"


@pytest.mark.django_db
def test_callback_rejects_invalid_state_without_contacting_wecom(client):
    set_oauth_session(client, state="expected-state")

    response = client.get(
        reverse("accounts:wecom_callback"), {"state": "wrong-state", "code": "abc"}
    )

    assert response.status_code == 403
    assert "登录失败，请联系管理员。" in response.content.decode()
    assert "expected-state" not in client.session


@pytest.mark.django_db
def test_callback_persisted_state_is_consumed_once_before_identity_exchange(client, monkeypatch):
    from apps.accounts.models import WeComOAuthState

    set_oauth_session(client)
    fake_client = FakeWeComClient("wx_missing")
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: fake_client)

    first = client.get(reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "one"})
    second = client.get(reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "two"})

    assert first.status_code == 403
    assert second.status_code == 403
    assert fake_client.codes == ["one"]
    assert WeComOAuthState.objects.get().consumed_at is not None


@pytest.mark.django_db
def test_callback_rejects_expired_persisted_state_without_identity_exchange(client, monkeypatch):
    set_oauth_session(client, expires_at=timezone.now() - timedelta(seconds=1))
    fake_client = FakeWeComClient()
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: fake_client)

    response = client.get(
        reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "expired"}
    )

    assert response.status_code == 403
    assert fake_client.codes == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_callbacks_with_independent_connections_consume_state_once(monkeypatch):
    from apps.accounts.models import WeComOAuthState

    category = EmployeeCategory.objects.create(code="CONCURRENT", name="Concurrent")
    Employee.objects.create(
        employee_no="E_CONCURRENT",
        name="Concurrent Employee",
        corporate_email="concurrent@example.test",
        category=category,
        department_level_1="Test",
        department_level_2="Test",
        wecom_userid="wx_concurrent",
    )
    seed_client = Client()
    set_oauth_session(seed_client, state="concurrent-state")
    session_key = seed_client.session.session_key
    fake_client = FakeWeComClient("wx_concurrent")
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: fake_client)

    request_barrier = Barrier(2)
    state_query_barrier = Barrier(2)
    state_update_barrier = Barrier(2)
    state_query_count = 0
    state_query_count_lock = Lock()
    original_select_for_update = QuerySet.select_for_update
    original_update = QuerySet.update

    def synchronized_select_for_update(queryset, *args, **kwargs):
        nonlocal state_query_count
        should_wait = False
        if queryset.model is WeComOAuthState:
            with state_query_count_lock:
                state_query_count += 1
                should_wait = state_query_count <= 2
        if should_wait:
            state_query_barrier.wait(timeout=10)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", synchronized_select_for_update)

    def synchronized_update(queryset, *args, **kwargs):
        if (
            queryset.model is WeComOAuthState
            and not connection.features.has_select_for_update
        ):
            state_update_barrier.wait(timeout=10)
        return original_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "update", synchronized_update)

    def callback_from_independent_connection():
        close_old_connections()
        try:
            thread_client = Client()
            thread_client.cookies[settings.SESSION_COOKIE_NAME] = session_key
            request_barrier.wait(timeout=10)
            return thread_client.get(
                reverse("accounts:wecom_callback"),
                {"state": "concurrent-state", "code": "concurrent-code"},
            ).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: callback_from_independent_connection(), range(2)))

    assert sorted(statuses) == [302, 403]
    assert fake_client.codes == ["concurrent-code"]


def test_wecom_client_uses_mock_transport_for_token_and_identity_exchange():
    from apps.accounts.wecom import WeComClient, WeComSettings

    requests = []

    def responder(request):
        requests.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "token-for-test", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "userid": "wx_e101"})

    client = WeComClient(
        WeComSettings(corp_id="corp-for-test", agent_id="agent-for-test", secret="secret-for-test"),
        transport=httpx.MockTransport(responder),
    )

    assert client.user_id_from_code("first-code") == "wx_e101"
    assert client.user_id_from_code("second-code") == "wx_e101"
    assert [request.url.path for request in requests] == [
        "/cgi-bin/gettoken",
        "/cgi-bin/auth/getuserinfo",
        "/cgi-bin/auth/getuserinfo",
    ]
    assert parse_qs(requests[1].url.query.decode())["code"] == ["first-code"]
    assert "token-for-test" not in client.authorization_url("state-for-test", "https://app.example/auth/wecom/callback/")


def test_wecom_authorization_url_has_oauth_parameters():
    from apps.accounts.wecom import WeComClient, WeComSettings

    url = WeComClient(
        WeComSettings(corp_id="corp-for-test", agent_id="agent-for-test", secret="secret-for-test"),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    ).authorization_url("state-for-test", "https://app.example/auth/wecom/callback/")

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "open.weixin.qq.com"
    assert parsed.path == "/connect/oauth2/authorize"
    assert query == {
        "appid": ["corp-for-test"],
        "redirect_uri": ["https://app.example/auth/wecom/callback/"],
        "response_type": ["code"],
        "scope": ["snsapi_base"],
        "state": ["state-for-test"],
    }
    assert parsed.fragment == "wechat_redirect"


def test_wecom_client_turns_malformed_identity_payload_into_a_safe_domain_error():
    from apps.accounts.wecom import WeComClient, WeComIdentityError, WeComSettings

    def responder(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "token-for-test", "expires_in": 7200})
        return httpx.Response(200, json=["not-an-identity-payload"])

    client = WeComClient(
        WeComSettings(corp_id="corp-for-test", agent_id="agent-for-test", secret="secret-for-test"),
        transport=httpx.MockTransport(responder),
    )

    with pytest.raises(WeComIdentityError):
        client.user_id_from_code("code-for-test")


def test_wecom_client_rejects_external_contact_without_internal_user_id():
    from apps.accounts.wecom import WeComClient, WeComIdentityError, WeComSettings

    def responder(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": "token-for-test", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "open_userid": "external-contact"})

    client = WeComClient(
        WeComSettings(corp_id="corp-for-test", agent_id="agent-for-test", secret="secret-for-test"),
        transport=httpx.MockTransport(responder),
    )

    with pytest.raises(WeComIdentityError):
        client.user_id_from_code("code-for-test")


def test_wecom_client_reuses_process_local_token_cache_across_instances():
    from apps.accounts.wecom import WeComClient, WeComSettings

    token_requests = 0

    def responder(request):
        nonlocal token_requests
        if request.url.path.endswith("/gettoken"):
            token_requests += 1
            return httpx.Response(200, json={"errcode": 0, "access_token": "shared-token", "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "userid": "wx_e101"})

    client_settings = WeComSettings(
        corp_id="process-cache-corp", agent_id="agent-for-test", secret="secret-for-test"
    )
    transport = httpx.MockTransport(responder)

    assert WeComClient(client_settings, transport=transport).user_id_from_code("first-code") == "wx_e101"
    assert WeComClient(client_settings, transport=transport).user_id_from_code("second-code") == "wx_e101"
    assert token_requests == 1


def test_wecom_http_logs_redact_query_parameter_secrets(caplog):
    from apps.accounts.wecom import WeComClient, WeComSettings

    secret = "secret-for-log-regression"
    token = "token-for-log-regression"
    code = "code-for-log-regression"

    def responder(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(200, json={"errcode": 0, "access_token": token, "expires_in": 7200})
        return httpx.Response(200, json={"errcode": 0, "userid": "wx_e101"})

    client = WeComClient(
        WeComSettings(corp_id="log-redaction-corp", agent_id="agent-for-test", secret=secret),
        transport=httpx.MockTransport(responder),
    )

    with caplog.at_level(logging.INFO, logger="httpx"):
        assert client.user_id_from_code(code) == "wx_e101"

    assert secret not in caplog.text
    assert token not in caplog.text
    assert code not in caplog.text


def test_wecom_http_errors_do_not_retain_sensitive_http_exception_cause():
    from apps.accounts.wecom import WeComClient, WeComIdentityError, WeComSettings

    client = WeComClient(
        WeComSettings(corp_id="cause-redaction-corp", agent_id="agent-for-test", secret="secret-for-cause"),
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )

    with pytest.raises(WeComIdentityError) as raised:
        client.user_id_from_code("code-for-cause")

    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "/\\attacker.example/path",
        "//attacker.example/path",
        "https://attacker.example/path",
        "/\x00control-character",
    ],
)
def test_safe_next_rejects_browser_normalized_and_unsafe_urls(unsafe_next):
    from apps.accounts.views import _safe_next

    assert _safe_next(unsafe_next) == "/"
