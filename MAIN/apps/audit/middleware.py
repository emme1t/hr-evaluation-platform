from django.core.cache import cache
from django.utils import timezone
from uuid import uuid4

from .services import record_audit, reset_correlation_id, set_correlation_id


DENIAL_LIMIT_PER_MINUTE = 5


class AuditRequestMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        correlation_id = str(uuid4())
        token = set_correlation_id(correlation_id)
        before_user_id = (
            request.user.pk
            if getattr(request.user, "is_authenticated", False)
            else None
        )
        try:
            response = self.get_response(request)
            if response.status_code == 403 and request.path.startswith("/hr/"):
                self._record_denial(request, correlation_id)
            elif before_user_id is None and getattr(
                request.user, "is_authenticated", False
            ):
                self._record_login(request, correlation_id)
            return response
        finally:
            reset_correlation_id(token)

    def _record_login(self, request, correlation_id):
        try:
            record_audit(
                request.user,
                "AUTH_LOGIN_SUCCEEDED",
                request.user,
                {"authentication": "completed"},
                idempotency_key=f"login:{request.user.public_id}:{correlation_id}",
                correlation_id=correlation_id,
            )
        except Exception:
            return

    def _record_denial(self, request, correlation_id):
        actor = request.user
        if not getattr(actor, "is_authenticated", False):
            return
        bucket = timezone.now().strftime("%Y%m%d%H%M")
        key = f"audit-denial:{getattr(actor, 'public_id', actor.pk)}:{bucket}"
        try:
            cache.add(key, 0, timeout=70)
            count = cache.incr(key)
        except (ValueError, NotImplementedError):
            count = int(cache.get(key, 0)) + 1
            cache.set(key, count, timeout=70)
        if count > DENIAL_LIMIT_PER_MINUTE:
            return
        try:
            record_audit(
                actor,
                "AUTH_ACCESS_DENIED",
                actor,
                {"endpoint_group": "hr", "reason": "forbidden"},
                idempotency_key=(
                    f"denial:{getattr(actor, 'public_id', actor.pk)}:{bucket}:{count}"
                ),
                correlation_id=correlation_id,
            )
        except Exception:
            return
