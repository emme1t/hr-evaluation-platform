import hmac
from hashlib import sha256
import logging
import secrets
import unicodedata
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.db import OperationalError, connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone

from apps.roster.models import Employee

from .wecom import WeComClient, WeComIdentityError, WeComSettings
from .models import WeComOAuthState


logger = logging.getLogger(__name__)
ERROR_TEMPLATE = "accounts/login_error.html"
SAFE_DEFAULT_NEXT = "/"
OAUTH_STATE_TTL = timedelta(seconds=settings.WECOM_OAUTH_STATE_TTL_SECONDS)


def get_wecom_client() -> WeComClient:
    return WeComClient(
        WeComSettings(
            corp_id=settings.WECOM_CORP_ID,
            agent_id=settings.WECOM_AGENT_ID,
            secret=settings.WECOM_SECRET,
        )
    )


def _safe_next(value: str | None) -> str:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
        or not url_has_allowed_host_and_scheme(value, allowed_hosts=set())
    ):
        return SAFE_DEFAULT_NEXT
    return value


def _login_error(request: HttpRequest, event: str) -> HttpResponse:
    logger.warning(event)
    return render(request, ERROR_TEMPLATE, status=403)


def _state_digest(state: str) -> str:
    return sha256(state.encode()).hexdigest()


def _consume_oauth_state(request: HttpRequest, state: str) -> bool:
    if not request.session.session_key or not state:
        return False

    now = timezone.now()
    returned_digest = _state_digest(state)
    active_states = WeComOAuthState.objects.filter(
        session_key=request.session.session_key,
        consumed_at__isnull=True,
        expires_at__gt=now,
    )

    def claim_state(candidates) -> bool:
        stored_state = next(
            (
                candidate
                for candidate in candidates
                if hmac.compare_digest(candidate.state_digest, returned_digest)
            ),
            None,
        )
        if stored_state is None:
            return False
        return (
            active_states.filter(pk=stored_state.pk, consumed_at__isnull=True).update(
                consumed_at=now
            )
            == 1
        )

    if connection.features.has_select_for_update:
        with transaction.atomic():
            return claim_state(active_states.select_for_update())

    try:
        return claim_state(active_states)
    except OperationalError:
        return False


def wecom_start(request: HttpRequest) -> HttpResponse:
    if getattr(settings, "APP_ENV", "local") == "offline":
        return _login_error(request, "wecom_oauth_offline_disabled")
    state = secrets.token_urlsafe(32)
    request.session["wecom_oauth_next"] = _safe_next(request.GET.get("next"))
    request.session.save()
    WeComOAuthState.objects.create(
        state_digest=_state_digest(state),
        session_key=request.session.session_key,
        expires_at=timezone.now() + OAUTH_STATE_TTL,
    )
    redirect_uri = request.build_absolute_uri(reverse("accounts:wecom_callback"))
    return HttpResponseRedirect(get_wecom_client().authorization_url(state, redirect_uri))


def wecom_callback(request: HttpRequest) -> HttpResponse:
    returned_state = request.GET.get("state", "")
    if not _consume_oauth_state(request, returned_state):
        return _login_error(request, "wecom_oauth_invalid_state")

    next_path = _safe_next(request.session.pop("wecom_oauth_next", None))

    code = request.GET.get("code", "")
    if not code:
        return _login_error(request, "wecom_oauth_missing_code")

    try:
        wecom_user_id = get_wecom_client().user_id_from_code(code)
    except Exception:
        return _login_error(request, "wecom_oauth_identity_fetch_failed")

    with transaction.atomic():
        try:
            employee = Employee.objects.select_for_update().get(wecom_userid=wecom_user_id)
        except Employee.DoesNotExist:
            return _login_error(request, "wecom_oauth_unbound_identity")

        if not employee.is_active:
            return _login_error(request, "wecom_oauth_inactive_employee")

        if employee.user_id is None:
            user = get_user_model().objects.create_user(
                username=f"wecom-{employee.public_id}",
                email=employee.corporate_email,
                password=None,
            )
            employee.user = user
            employee.save(update_fields=["user"])

    login(request, employee.user)
    return HttpResponseRedirect(next_path)
