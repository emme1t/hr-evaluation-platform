from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
import re
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from apps.audit.services import record_audit
from apps.core.permissions import require_hr_actor
from apps.evaluations.models import EvaluationProject, EvaluationTask
from apps.roster.models import Employee
from config.settings.validation import validate_public_base_url

from .email import SUBJECT as EMAIL_SUBJECT
from .email import build_task_summary_email_body, send_task_summary_email
from .models import EmailMagicLink, NotificationAttempt, NotificationOutbox
from .wecom import WeComNotifier


STABLE_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,79}\Z")
PAYLOAD_SNAPSHOT_VERSION = 1
EMAIL_LINK_PLACEHOLDER = "[一次性链接]"
DELIVERY_IN_PROGRESS = "DELIVERY_IN_PROGRESS"
DELIVERY_UNCERTAIN = "NOTIFICATION_DELIVERY_UNCERTAIN"


class NotificationError(ValueError):
    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class NotificationStateError(NotificationError):
    pass


class InvalidNotificationChannel(NotificationError):
    def __init__(self):
        super().__init__("通知通道无效", "INVALID_NOTIFICATION_CHANNEL")


class InvalidMagicLink(NotificationError):
    def __init__(self):
        super().__init__("链接无效或已过期", "INVALID_MAGIC_LINK")


@dataclass(frozen=True)
class LaunchResult:
    sent_recipient_count: int
    already_launched: bool


def _token_hash(raw_token):
    return sha256(raw_token.encode("utf-8")).hexdigest()


def validated_public_base_url():
    return validate_public_base_url(
        getattr(settings, "EVALUATION_PUBLIC_BASE_URL", ""),
        app_env=getattr(settings, "APP_ENV", ""),
        allowed_hosts=getattr(settings, "ALLOWED_HOSTS", ()),
    )


def _task_center_url(project):
    return f"{validated_public_base_url()}/projects/{project.public_id}/tasks/"


def _delivery_tasks(project, evaluator):
    return list(
        EvaluationTask.objects.filter(
            project=project,
            evaluator=evaluator,
            status=EvaluationTask.Status.PENDING,
        )
        .select_related("subject", "project_subject")
        .order_by("pk")
    )


def _digest(project, evaluator, tasks):
    material = "|".join(
        [str(project.public_id), str(evaluator.public_id)]
        + [str(task.public_id) for task in tasks]
    )
    return sha256(material.encode("ascii")).hexdigest()


def _validate_channel(channel):
    if channel not in NotificationAttempt.Channel.values:
        raise InvalidNotificationChannel()


def _prepare_attempt(project, evaluator, channel, *, reuse_queued=False):
    _validate_channel(channel)
    with transaction.atomic():
        project = EvaluationProject.objects.select_for_update(of=("self",)).get(
            pk=project.pk
        )
        if project.status != EvaluationProject.Status.ACTIVE:
            raise NotificationStateError("项目尚未进行", "PROJECT_NOT_ACTIVE")
        if project.deadline <= timezone.now():
            raise NotificationStateError("项目已过截止时间", "PROJECT_EXPIRED")
        tasks = _delivery_tasks(project, evaluator)
        if not tasks:
            raise NotificationStateError("没有待评价任务", "NO_PENDING_TASKS")
        task_ids = [task.pk for task in tasks]
        task_digest = _digest(project, evaluator, tasks)
        try:
            outbox = NotificationOutbox.objects.select_for_update().get(
                project=project, recipient=evaluator
            )
        except NotificationOutbox.DoesNotExist:
            try:
                with transaction.atomic():
                    outbox = NotificationOutbox.objects.create(
                        project=project,
                        recipient=evaluator,
                        task_count=len(tasks),
                        task_digest=task_digest,
                    )
            except IntegrityError:
                outbox = NotificationOutbox.objects.select_for_update().get(
                    project=project, recipient=evaluator
                )
            else:
                outbox.tasks.add(*tasks)

        existing_ids = list(outbox.tasks.order_by("pk").values_list("pk", flat=True))
        if existing_ids != task_ids or outbox.task_digest != task_digest:
            raise NotificationStateError(
                "通知任务集合已变化", "NOTIFICATION_TASK_SET_CHANGED"
            )
        latest = outbox.attempts.order_by("-attempt").first()
        if (
            reuse_queued
            and latest is not None
            and latest.status == NotificationAttempt.Status.QUEUED
            and latest.channel == channel
        ):
            return latest
        next_number = (
            outbox.attempts.aggregate(value=Max("attempt"))["value"] or 0
        ) + 1
        attempt = NotificationAttempt.objects.create(
            outbox=outbox,
            project=project,
            recipient=evaluator,
            task_count=len(tasks),
            channel=channel,
            attempt=next_number,
            payload_snapshot=_build_payload_snapshot(
                channel=channel,
                project=project,
                recipient=evaluator,
                tasks=tasks,
            ),
        )
        attempt.tasks.add(*tasks)
        if outbox.status != NotificationOutbox.Status.QUEUED:
            outbox.status = NotificationOutbox.Status.QUEUED
            outbox.save(update_fields=["status", "updated_at"])
    return attempt


def _failure_code(exc, channel):
    candidate = getattr(exc, "code", "")
    if isinstance(candidate, str) and STABLE_CODE.fullmatch(candidate):
        return candidate
    return "WECOM_DELIVERY_FAILED" if channel == "wecom" else "EMAIL_DELIVERY_FAILED"


def _wecom_delivery_content(project, recipient, tasks):
    return {
        "title": "您有新的评价任务",
        "description": (
            f"您好，{recipient.name}\n"
            f"项目：{project.name}\n"
            f"您有 {len(tasks)} 项待评价任务\n"
            f"截止时间：{timezone.localtime(project.deadline):%Y-%m-%d %H:%M}"
        ),
        "url": _task_center_url(project),
    }


def _unavailable_payload(channel):
    return {
        "version": PAYLOAD_SNAPSHOT_VERSION,
        "channel": channel,
        "unavailable": True,
    }


def _build_payload_snapshot(*, channel, project, recipient, tasks):
    if channel == NotificationAttempt.Channel.WECOM:
        return {
            "version": PAYLOAD_SNAPSHOT_VERSION,
            "channel": "wecom",
            **_wecom_delivery_content(project, recipient, tasks),
        }
    placeholder_url = (
        f"{validated_public_base_url()}"
        f"{reverse('notifications:email-magic-link')}#{EMAIL_LINK_PLACEHOLDER}"
    )
    subject_names = [_subject_display_name(task) for task in tasks]
    return {
        "version": PAYLOAD_SNAPSHOT_VERSION,
        "channel": "email",
        "subject": EMAIL_SUBJECT,
        "body": build_task_summary_email_body(
            recipient=recipient,
            project=project,
            subject_names=subject_names,
            url=placeholder_url,
        ),
        "attachments": [],
        "link_placeholder_url": placeholder_url,
    }


def notification_attempt_preview(attempt):
    payload = attempt.payload_snapshot
    if not isinstance(payload, dict):
        return _unavailable_payload(attempt.channel)
    if (
        payload.get("version") != PAYLOAD_SNAPSHOT_VERSION
        or payload.get("channel") != attempt.channel
    ):
        return _unavailable_payload(attempt.channel)
    if payload.get("unavailable") is True:
        return _unavailable_payload(attempt.channel)
    if attempt.channel == NotificationAttempt.Channel.WECOM:
        if not all(isinstance(payload.get(key), str) for key in ("title", "description", "url")):
            return _unavailable_payload(attempt.channel)
        return payload
    if attempt.channel == NotificationAttempt.Channel.EMAIL:
        required_strings = ("subject", "body", "link_placeholder_url")
        if not all(isinstance(payload.get(key), str) for key in required_strings):
            return _unavailable_payload(attempt.channel)
        placeholder_url = payload["link_placeholder_url"]
        if (
            payload.get("attachments") != []
            or not placeholder_url.endswith(f"#{EMAIL_LINK_PLACEHOLDER}")
            or payload["body"].count(placeholder_url) != 1
        ):
            return _unavailable_payload(attempt.channel)
        return {**payload, "has_attachment": False}
    return _unavailable_payload(attempt.channel)


def _audit_delivery_outcome(attempt, project, actor):
    if actor is None:
        return
    action = (
        "NOTIFICATION_SENT"
        if attempt.status == NotificationAttempt.Status.SENT
        else "NOTIFICATION_FAILED"
    )
    record_audit(
        actor,
        action,
        attempt,
        {
            "channel": attempt.channel,
            "status": attempt.status,
            "failure_code": attempt.failure_code,
        },
        idempotency_key=f"notification:{attempt.public_id}:{attempt.status}",
        project=project,
    )


def _finish_locked_attempt(
    attempt, outbox, project, status, code, actor, *, update_outbox=True
):
    attempt.status = status
    attempt.failure_code = code
    attempt.completed_at = timezone.now()
    attempt.save(update_fields=["status", "failure_code", "completed_at"])
    if update_outbox:
        outbox.status = status
        outbox.save(update_fields=["status", "updated_at"])
    _audit_delivery_outcome(attempt, project, actor)
    return attempt


@transaction.atomic
def _reserve_delivery(attempt_id, actor=None):
    attempt_reference = NotificationAttempt.objects.only(
        "outbox_id", "project_id"
    ).get(pk=attempt_id)
    project = EvaluationProject.objects.select_for_update(of=("self",)).get(
        pk=attempt_reference.project_id
    )
    outbox = NotificationOutbox.objects.select_for_update().get(
        pk=attempt_reference.outbox_id, project=project
    )
    attempt = (
        NotificationAttempt.objects.select_for_update()
        .select_related("recipient", "project")
        .get(pk=attempt_id, outbox=outbox)
    )
    if attempt.status != NotificationAttempt.Status.QUEUED:
        return attempt, False
    if attempt.failure_code == DELIVERY_IN_PROGRESS:
        raise NotificationStateError(
            "通知发送结果待人工确认", DELIVERY_UNCERTAIN
        )
    latest_id = (
        outbox.attempts.order_by("-attempt").values_list("pk", flat=True).first()
    )
    if latest_id != attempt.pk:
        return (
            _finish_locked_attempt(
                attempt,
                outbox,
                project,
                NotificationAttempt.Status.FAILED,
                "NOTIFICATION_SUPERSEDED",
                actor,
                update_outbox=False,
            ),
            False,
        )
    if project.status != EvaluationProject.Status.ACTIVE:
        return (
            _finish_locked_attempt(
                attempt,
                outbox,
                project,
                NotificationAttempt.Status.FAILED,
                "PROJECT_NOT_ACTIVE",
                actor,
            ),
            False,
        )
    if project.deadline <= timezone.now():
        return (
            _finish_locked_attempt(
                attempt,
                outbox,
                project,
                NotificationAttempt.Status.FAILED,
                "PROJECT_EXPIRED",
                actor,
            ),
            False,
        )
    attempt.failure_code = DELIVERY_IN_PROGRESS
    attempt.completed_at = None
    attempt.save(update_fields=["failure_code", "completed_at"])
    return attempt, True


def _call_delivery_provider(attempt, notifier=None):
    recipient = attempt.recipient
    project = attempt.project
    payload = notification_attempt_preview(attempt)
    if payload.get("unavailable"):
        error = ValueError("NOTIFICATION_PAYLOAD_UNAVAILABLE")
        error.code = "NOTIFICATION_PAYLOAD_UNAVAILABLE"
        raise error
    if attempt.channel == NotificationAttempt.Channel.WECOM:
        if not recipient.wecom_userid:
            error = ValueError("WECOM_NOT_BOUND")
            error.code = "WECOM_NOT_BOUND"
            raise error
        (notifier or WeComNotifier()).send_task_summary(
            userid=recipient.wecom_userid,
            title=payload["title"],
            description=payload["description"],
            url=payload["url"],
            attempt_public_id=str(attempt.public_id),
        )
        return
    if not recipient.corporate_email:
        error = ValueError("EMAIL_ADDRESS_MISSING")
        error.code = "EMAIL_ADDRESS_MISSING"
        raise error
    raw_token, link = create_email_magic_link(project, recipient)
    try:
        placeholder_url = payload["link_placeholder_url"]
        actual_url = placeholder_url[: -len(EMAIL_LINK_PLACEHOLDER)] + raw_token
        send_task_summary_email(
            recipient_email=recipient.corporate_email,
            subject=payload["subject"],
            body=payload["body"].replace(placeholder_url, actual_url, 1),
            attempt_public_id=str(attempt.public_id),
        )
    except Exception:
        EmailMagicLink.objects.filter(
            pk=link.pk, used_at__isnull=True
        ).update(revoked_at=timezone.now())
        raise


@transaction.atomic
def _finalize_delivery(attempt_id, status, code, actor=None):
    attempt_reference = NotificationAttempt.objects.only(
        "outbox_id", "project_id"
    ).get(pk=attempt_id)
    project = EvaluationProject.objects.select_for_update(of=("self",)).get(
        pk=attempt_reference.project_id
    )
    outbox = NotificationOutbox.objects.select_for_update().get(
        pk=attempt_reference.outbox_id, project=project
    )
    attempt = NotificationAttempt.objects.select_for_update().get(
        pk=attempt_id, outbox=outbox
    )
    if (
        attempt.status != NotificationAttempt.Status.QUEUED
        or attempt.failure_code != DELIVERY_IN_PROGRESS
    ):
        raise NotificationStateError(
            "通知发送结果待人工确认", DELIVERY_UNCERTAIN
        )
    return _finish_locked_attempt(attempt, outbox, project, status, code, actor)


def _deliver_attempt(attempt_id, notifier=None, actor=None):
    attempt, reserved = _reserve_delivery(attempt_id, actor)
    if not reserved:
        return attempt
    try:
        _call_delivery_provider(attempt, notifier)
    except Exception as exc:
        status = NotificationAttempt.Status.FAILED
        code = _failure_code(exc, attempt.channel)
    else:
        status = NotificationAttempt.Status.SENT
        code = ""
    return _finalize_delivery(attempt.pk, status, code, actor)


def _subject_display_name(task):
    snapshot = task.project_subject.subject_snapshot
    if isinstance(snapshot, dict):
        name = snapshot.get("name")
        if isinstance(name, str) and name.strip():
            return name
    return task.subject.name


def notify_project_evaluator(project, evaluator, channel, notifier=None):
    project = EvaluationProject.objects.get(pk=project.pk)
    evaluator = Employee.objects.get(pk=evaluator.pk)
    attempt = _prepare_attempt(project, evaluator, channel, reuse_queued=True)
    return _deliver_attempt(attempt.pk, notifier)


def launch_project(project, actor, notifier=None):
    require_hr_actor(actor)
    with transaction.atomic():
        locked = EvaluationProject.objects.select_for_update(of=("self",)).get(
            pk=project.pk
        )
        if (
            locked.status == EvaluationProject.Status.ACTIVE
            and locked.launched_at is not None
        ):
            return LaunchResult(sent_recipient_count=0, already_launched=True)
        if locked.status != EvaluationProject.Status.READY:
            raise NotificationStateError("项目不是待发送状态", "PROJECT_NOT_READY")
        locked.status = EvaluationProject.Status.ACTIVE
        locked.launched_at = timezone.now()
        locked.save(update_fields=["status", "launched_at"])
        evaluator_ids = list(
            locked.tasks.filter(status=EvaluationTask.Status.PENDING)
            .order_by("evaluator_id")
            .values_list("evaluator_id", flat=True)
            .distinct()
        )
        attempts = [
            _prepare_attempt(locked, Employee.objects.get(pk=evaluator_id), "wecom")
            for evaluator_id in evaluator_ids
        ]
        record_audit(
            actor,
            "PROJECT_LAUNCHED",
            locked,
            {"recipient_count": len(attempts)},
            idempotency_key=f"project:{locked.public_id}:launched",
        )
        for attempt in attempts:
            transaction.on_commit(
                lambda attempt_id=attempt.pk: _deliver_attempt(
                    attempt_id, notifier, actor
                )
            )
    return LaunchResult(
        sent_recipient_count=len(attempts), already_launched=False
    )


def resend_project_notification(
    project, evaluator, channel, actor, notifier=None, expected_attempt=None
):
    require_hr_actor(actor)
    _validate_channel(channel)
    with transaction.atomic():
        is_retry = False
        project = EvaluationProject.objects.select_for_update(of=("self",)).get(
            pk=project.pk
        )
        if (
            project.status != EvaluationProject.Status.ACTIVE
            or project.deadline <= timezone.now()
        ):
            raise NotificationStateError("项目不允许重发", "PROJECT_NOT_ACTIVE")
        evaluator = Employee.objects.get(pk=evaluator.pk)
        try:
            outbox = NotificationOutbox.objects.select_for_update().get(
                project=project, recipient=evaluator
            )
        except NotificationOutbox.DoesNotExist:
            attempt = _prepare_attempt(
                project, evaluator, channel, reuse_queued=True
            )
        else:
            latest = outbox.attempts.select_for_update().order_by("-attempt").first()
            if latest is not None and not expected_attempt:
                raise NotificationStateError(
                    "通知状态校验缺失，请刷新后重试",
                    "STALE_NOTIFICATION_ATTEMPT",
                )
            if latest is not None and str(latest.public_id) != str(expected_attempt):
                raise NotificationStateError(
                    "通知状态已更新，请刷新后重试",
                    "STALE_NOTIFICATION_ATTEMPT",
                )
            is_retry = latest is not None
            if latest is not None and latest.failure_code == DELIVERY_IN_PROGRESS:
                raise NotificationStateError(
                    "通知发送结果待人工确认", DELIVERY_UNCERTAIN
                )
            if latest is not None and latest.status == NotificationAttempt.Status.SENT:
                raise NotificationStateError(
                    "通知已发送成功", "NOTIFICATION_NOT_RETRYABLE"
                )
            if latest is not None and latest.status == NotificationAttempt.Status.QUEUED:
                if latest.channel == channel:
                    attempt = latest
                else:
                    latest.status = NotificationAttempt.Status.FAILED
                    latest.failure_code = "NOTIFICATION_SUPERSEDED"
                    latest.completed_at = timezone.now()
                    latest.save(
                        update_fields=["status", "failure_code", "completed_at"]
                    )
                    attempt = _prepare_attempt(project, evaluator, channel)
            else:
                attempt = _prepare_attempt(project, evaluator, channel)
        if is_retry:
            record_audit(
                actor,
                "NOTIFICATION_RETRIED",
                attempt,
                {"channel": attempt.channel, "status": "queued", "intent": "retry"},
                idempotency_key=f"notification:{attempt.public_id}:retried",
                project=project,
            )
    return _deliver_attempt(attempt.pk, notifier, actor)


def create_email_magic_link(project, evaluator, ttl_hours=24):
    raw_token = secrets.token_urlsafe(32)
    link = EmailMagicLink.objects.create(
        token_hash=_token_hash(raw_token),
        recipient=evaluator,
        project=project,
        expires_at=timezone.now() + timedelta(hours=ttl_hours),
    )
    return raw_token, link


def _valid_link_queryset(raw_token, *, lock=False):
    queryset = EmailMagicLink.objects.select_related("recipient", "project")
    if lock:
        queryset = queryset.select_for_update()
    return queryset.filter(token_hash=_token_hash(raw_token)).first()


def _link_is_valid(link, now):
    return bool(
        link
        and link.used_at is None
        and link.revoked_at is None
        and link.expires_at > now
        and link.recipient.is_active
        and (link.recipient.user_id is None or link.recipient.user.is_active)
        and link.project.status == EvaluationProject.Status.ACTIVE
        and link.project.deadline > now
    )


def validate_email_magic_link(raw_token):
    link = _valid_link_queryset(raw_token)
    if not _link_is_valid(link, timezone.now()):
        raise InvalidMagicLink()
    return link


def consume_email_magic_link(raw_token):
    with transaction.atomic():
        link = _valid_link_queryset(raw_token, lock=True)
        now = timezone.now()
        if not _link_is_valid(link, now):
            raise InvalidMagicLink()
        updated = EmailMagicLink.objects.filter(
            pk=link.pk, used_at__isnull=True, revoked_at__isnull=True
        ).update(used_at=now)
        if updated != 1:
            raise InvalidMagicLink()
        link.used_at = now
        return link


def consume_magic_link_and_bind_user(raw_token):
    with transaction.atomic():
        link = _valid_link_queryset(raw_token, lock=True)
        now = timezone.now()
        if not _link_is_valid(link, now):
            raise InvalidMagicLink()
        recipient = Employee.objects.select_for_update().get(pk=link.recipient_id)
        if not recipient.is_active:
            raise InvalidMagicLink()
        if recipient.user_id is None:
            user = get_user_model().objects.create_user(
                username=f"email-{recipient.public_id}",
                email=recipient.corporate_email,
                password=None,
            )
            recipient.user = user
            recipient.save(update_fields=["user"])
        elif not recipient.user.is_active:
            raise InvalidMagicLink()
        updated = EmailMagicLink.objects.filter(
            pk=link.pk, used_at__isnull=True, revoked_at__isnull=True
        ).update(used_at=now)
        if updated != 1:
            raise InvalidMagicLink()
        return recipient.user
