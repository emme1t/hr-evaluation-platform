from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.views.debug import SafeExceptionReporterFilter
from django.db import close_old_connections, connection
from django.test import RequestFactory
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import EmailMagicLink
from apps.notifications.services import (
    InvalidMagicLink,
    consume_email_magic_link,
    create_email_magic_link,
    validate_email_magic_link,
)


@pytest.mark.django_db
def test_email_magic_link_is_hashed_expires_and_can_only_be_used_once(
    evaluator, active_project
):
    raw_token, link = create_email_magic_link(active_project, evaluator, ttl_hours=24)

    assert raw_token not in link.token_hash
    assert len(link.token_hash) == 64
    assert link.expires_at > timezone.now()
    assert validate_email_magic_link(raw_token).id == link.id
    link.refresh_from_db()
    assert link.used_at is None
    assert consume_email_magic_link(raw_token).id == link.id
    with pytest.raises(InvalidMagicLink):
        consume_email_magic_link(raw_token)


@pytest.mark.django_db
def test_token_free_magic_link_get_confirms_without_consuming_and_body_post_logs_in(
    client, evaluator, active_project
):
    raw_token, link = create_email_magic_link(active_project, evaluator)
    url = reverse("notifications:email-magic-link")

    confirmation = client.get(url)
    link.refresh_from_db()
    assert confirmation.status_code == 200
    assert link.used_at is None
    assert confirmation.headers["Cache-Control"] == "no-store"
    assert confirmation.headers["Referrer-Policy"] == "no-referrer"

    accepted = client.post(url, {"token": raw_token})
    evaluator.refresh_from_db()
    link.refresh_from_db()
    assert accepted.status_code == 302
    assert accepted.url == reverse("evaluations:task_list")
    assert link.used_at is not None
    assert evaluator.user_id is not None
    assert evaluator.user.has_usable_password() is False
    assert client.session["_auth_user_id"] == str(evaluator.user_id)


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["unknown", "expired", "used", "inactive"])
def test_magic_link_post_failures_are_uniform_and_do_not_reveal_identity(
    client, evaluator, active_project, case
):
    raw_token, link = create_email_magic_link(active_project, evaluator)
    if case == "unknown":
        raw_token = "unknown-high-entropy-token"
    elif case == "expired":
        EmailMagicLink.objects.filter(pk=link.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    elif case == "used":
        consume_email_magic_link(raw_token)
    else:
        evaluator.is_active = False
        evaluator.save(update_fields=["is_active"])

    response = client.post(reverse("notifications:email-magic-link"), {"token": raw_token})
    body = response.content.decode("utf-8")
    assert response.status_code == 403
    assert "链接无效或已过期" in body
    assert evaluator.name not in body
    assert evaluator.corporate_email not in body
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.django_db(transaction=True)
def test_postgresql_concurrent_magic_link_consumption_succeeds_at_most_once(
    evaluator, active_project
):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only row-lock evidence; SQLite is not proof")
    raw_token, _ = create_email_magic_link(active_project, evaluator)

    def consume_once():
        close_old_connections()
        try:
            consume_email_magic_link(raw_token)
            return "consumed"
        except InvalidMagicLink:
            return "rejected"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume_once(), range(2)))
    assert results.count("consumed") == 1
    assert results.count("rejected") == 1


@pytest.mark.django_db
def test_magic_link_post_does_not_create_user_for_invalid_token(
    client, evaluator, active_project
):
    before = get_user_model().objects.count()
    response = client.post(
        reverse("notifications:email-magic-link"),
        {"token": "unknown-token"},
    )
    assert response.status_code == 403
    assert get_user_model().objects.count() == before


@pytest.mark.django_db
def test_fragment_token_never_enters_django_path_query_or_request_log(
    client, evaluator, active_project, caplog
):
    raw_token, link = create_email_magic_link(active_project, evaluator)
    url = reverse("notifications:email-magic-link")

    response = client.get(f"{url}#{raw_token}")

    link.refresh_from_db()
    assert response.status_code == 200
    assert response.wsgi_request.path == "/auth/email/confirm/"
    assert response.wsgi_request.META["QUERY_STRING"] == ""
    assert raw_token not in caplog.text
    assert link.used_at is None


@pytest.mark.django_db
def test_confirm_page_moves_fragment_to_hidden_post_body_without_external_script(
    client,
):
    response = client.get(reverse("notifications:email-magic-link"))
    html = response.content.decode("utf-8")

    assert 'name="token"' in html
    assert "window.location.hash" in html
    assert "history.replaceState" in html
    assert "<script src=" not in html
    assert 'id="magic-link-status"' in html
    assert "链接中缺少有效的一次性凭证" in html
    assert "^[A-Za-z0-9_-]{40,128}$" in html


@pytest.mark.django_db
def test_magic_link_post_without_fragment_token_is_rejected_with_human_message(client):
    response = client.post(reverse("notifications:email-magic-link"), {})
    assert response.status_code == 403
    assert "链接无效或已过期" in response.content.decode("utf-8")


@pytest.mark.django_db
def test_magic_link_post_token_is_redacted_by_safe_exception_reporter(
    monkeypatch, settings
):
    settings.DEBUG = False
    dummy_raw_token = "DummySensitiveToken_12345678901234567890"

    def unexpected_failure(raw_token):
        raise RuntimeError("fictional unexpected consume failure")

    monkeypatch.setattr(
        "apps.notifications.views.consume_magic_link_and_bind_user",
        unexpected_failure,
    )
    request = RequestFactory().post(
        reverse("notifications:email-magic-link"),
        {"token": dummy_raw_token},
    )

    from apps.notifications.views import email_magic_link

    with pytest.raises(RuntimeError):
        email_magic_link(request)

    safe_post = SafeExceptionReporterFilter().get_cleansed_multivaluedict(
        request, request.POST
    )
    assert safe_post["token"] != dummy_raw_token
    assert dummy_raw_token not in repr(safe_post)
