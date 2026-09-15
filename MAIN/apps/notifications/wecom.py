from dataclasses import dataclass

import httpx
from django.conf import settings

from apps.accounts.wecom import WeComClient, WeComSettings


class WeComDeliveryError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass
class WeComNotifier:
    client: WeComClient | None = None

    def _client(self):
        if getattr(settings, "APP_ENV", "local") != "production":
            raise WeComDeliveryError("WECOM_DISABLED")
        values = (
            settings.WECOM_CORP_ID,
            settings.WECOM_AGENT_ID,
            settings.WECOM_SECRET,
        )
        if not all(values):
            raise WeComDeliveryError("WECOM_NOT_CONFIGURED")
        return self.client or WeComClient(WeComSettings(*values))

    def send_task_summary(
        self, *, userid, title, description, url, attempt_public_id=None
    ):
        if not userid:
            raise WeComDeliveryError("WECOM_NOT_BOUND")
        client = self._client()
        payload = {
            "touser": userid,
            "msgtype": "textcard",
            "agentid": settings.WECOM_AGENT_ID,
            "enable_duplicate_check": 1,
            "duplicate_check_interval": 1800,
            "textcard": {
                "title": title,
                "description": description,
                "url": url,
                "btntxt": "进入任务中心",
            },
        }
        try:
            headers = (
                {"X-Notification-Attempt": str(attempt_public_id)}
                if attempt_public_id
                else None
            )
            response = client.client.post(
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": client._get_access_token()},
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException:
            raise WeComDeliveryError("WECOM_TIMEOUT") from None
        except (httpx.HTTPError, ValueError):
            raise WeComDeliveryError("WECOM_HTTP_ERROR") from None
        if not isinstance(body, dict) or body.get("errcode") != 0:
            raise WeComDeliveryError("WECOM_API_ERROR")
