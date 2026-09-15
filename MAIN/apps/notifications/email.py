import socket

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.utils import timezone


SUBJECT = "恩力公司人事部门人事评价项目"


class EmailDeliveryError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def build_task_summary_email_body(*, recipient, project, subject_names, url):
    names = "\n".join(f"- {name}" for name in subject_names)
    return (
        f"您好，{recipient.name}：\n\n"
        f"您在“{project.name}”中有以下待评价人员：\n{names}\n\n"
        f"截止时间：{timezone.localtime(project.deadline):%Y-%m-%d %H:%M}\n"
        f"请通过以下一次性安全链接进入任务中心：\n{url}\n"
    )


def send_task_summary_email(*, recipient_email, subject, body, attempt_public_id=None):
    if getattr(settings, "APP_ENV", "local") == "production" and (
        not getattr(settings, "EMAIL_HOST", "")
        or not getattr(settings, "DEFAULT_FROM_EMAIL", "")
    ):
        raise EmailDeliveryError("EMAIL_NOT_CONFIGURED")
    if not recipient_email:
        raise EmailDeliveryError("EMAIL_ADDRESS_MISSING")
    connection = get_connection(timeout=settings.EMAIL_TIMEOUT)
    message = EmailMessage(
        subject=subject,
        body=body,
        to=[recipient_email],
        connection=connection,
        headers=(
            {"X-Notification-Attempt": str(attempt_public_id)}
            if attempt_public_id
            else None
        ),
    )
    try:
        message.send(fail_silently=False)
    except (TimeoutError, socket.timeout):
        raise EmailDeliveryError("EMAIL_TIMEOUT") from None
