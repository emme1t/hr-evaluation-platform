from django.core.exceptions import ImproperlyConfigured
from django.core.mail.backends.base import BaseEmailBackend


class DisabledOutboundBackend(BaseEmailBackend):
    """Fail closed when offline code attempts to send external email."""

    def send_messages(self, email_messages):
        raise ImproperlyConfigured("离线模式禁止发送外部邮件")
