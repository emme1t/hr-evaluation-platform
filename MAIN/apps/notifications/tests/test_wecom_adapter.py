import json

import httpx
import pytest

from apps.accounts.wecom import WeComClient, WeComSettings
from apps.notifications.wecom import WeComDeliveryError, WeComNotifier


def _client(settings, handler):
    return WeComClient(settings, transport=httpx.MockTransport(handler))


def test_wecom_notifier_sends_textcard_through_injected_transport(settings):
    settings.APP_ENV = "production"
    settings.WECOM_CORP_ID = "fake-corp-send"
    settings.WECOM_AGENT_ID = "1000002"
    settings.WECOM_SECRET = "fake-secret"
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "fake-token", "expires_in": 7200},
            )
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok"})

    client = _client(
        WeComSettings("fake-corp-send", "1000002", "fake-secret"), handler
    )
    WeComNotifier(client).send_task_summary(
        userid="wx-fictional",
        title="您有新的评价任务",
        description="项目：虚构项目\n您有 2 项待评价任务\n截止时间：2026-09-01 18:00",
        url="https://evaluation.example.test/projects/00000000-0000-4000-8000-000000000001/tasks/",
    )

    message_request = requests[-1]
    payload = json.loads(message_request.content.decode("utf-8"))
    assert message_request.url.host == "qyapi.weixin.qq.com"
    assert payload["touser"] == "wx-fictional"
    assert payload["msgtype"] == "textcard"
    assert payload["textcard"]["btntxt"] == "进入任务中心"
    assert payload["textcard"]["url"].endswith(
        "/00000000-0000-4000-8000-000000000001/tasks/"
    )


@pytest.mark.parametrize(
    ("message_response", "expected_code"),
    [
        ({"errcode": 40014, "errmsg": "fictional invalid token"}, "WECOM_API_ERROR"),
        (None, "WECOM_TIMEOUT"),
    ],
)
def test_wecom_notifier_maps_external_failures_to_stable_codes(
    settings, message_response, expected_code
):
    settings.APP_ENV = "production"
    settings.WECOM_CORP_ID = f"fake-corp-{expected_code.lower()}"
    settings.WECOM_AGENT_ID = "1000002"
    settings.WECOM_SECRET = "fake-secret"

    def handler(request):
        if request.url.path.endswith("/gettoken"):
            return httpx.Response(
                200,
                json={"errcode": 0, "access_token": "fake-token", "expires_in": 7200},
            )
        if message_response is None:
            raise httpx.ReadTimeout("fictional timeout", request=request)
        return httpx.Response(200, json=message_response)

    client = _client(
        WeComSettings(
            settings.WECOM_CORP_ID, settings.WECOM_AGENT_ID, settings.WECOM_SECRET
        ),
        handler,
    )
    with pytest.raises(WeComDeliveryError) as exc_info:
        WeComNotifier(client).send_task_summary(
            userid="wx-fictional",
            title="您有新的评价任务",
            description="虚构通知",
            url="https://evaluation.example.test/projects/00000000-0000-4000-8000-000000000001/tasks/",
        )
    assert exc_info.value.code == expected_code
    assert str(exc_info.value) == expected_code
    assert "fake-token" not in str(exc_info.value)
    assert "fictional invalid token" not in str(exc_info.value)
