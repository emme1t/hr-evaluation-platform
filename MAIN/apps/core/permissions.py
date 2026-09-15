from functools import wraps

from django.contrib.auth import get_user_model
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.urls import reverse


HR_ROLE_NAMES = ("HR_ADMIN", "HR_OPERATOR")


class HRPermissionError(ValueError):
    def __init__(self, message: str, code: str = "HR_REQUIRED"):
        super().__init__(message)
        self.code = code


def current_actor_role(actor, allowed_roles=HR_ROLE_NAMES):
    if not actor or not getattr(actor, "is_authenticated", False):
        raise HRPermissionError("需要 HR 身份")
    try:
        fresh = get_user_model().objects.get(pk=actor.pk, is_active=True)
    except get_user_model().DoesNotExist as exc:
        raise HRPermissionError("需要 HR 权限") from exc
    role = next(
        (
            candidate
            for candidate in ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR")
            if candidate in allowed_roles
            and fresh.groups.filter(name=candidate).exists()
        ),
        None,
    )
    if role is None:
        raise HRPermissionError("需要 HR 权限")
    return fresh, role


def require_hr_actor(actor, allowed_roles=HR_ROLE_NAMES):
    return current_actor_role(actor, allowed_roles)


def require_hr_role(*allowed_roles):
    roles = allowed_roles or HR_ROLE_NAMES

    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(
                    request.get_full_path(),
                    login_url=reverse("accounts:wecom_start"),
                )
            try:
                actor, role = current_actor_role(request.user, roles)
            except HRPermissionError as exc:
                raise PermissionDenied from exc
            request.user = actor
            request.hr_role = role
            return view(request, *args, **kwargs)

        return wrapped

    return decorator
