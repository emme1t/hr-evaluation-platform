from dataclasses import dataclass
import logging
import re
from time import monotonic
from threading import Lock
from urllib.parse import urlencode

import httpx


_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_CACHE_LOCK = Lock()
_SENSITIVE_QUERY_PARAMETER = re.compile(
    r"([?&](?:access_token|code|corpsecret)=)[^&\s\"']+", re.IGNORECASE
)


class _SensitiveHttpLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted_message = _SENSITIVE_QUERY_PARAMETER.sub(r"\1[redacted]", message)
        if redacted_message != message:
            record.msg = redacted_message
            record.args = ()
        return True


def _install_sensitive_http_log_filters() -> None:
    for logger_name in ("httpx", "httpcore"):
        target_logger = logging.getLogger(logger_name)
        if not any(
            isinstance(log_filter, _SensitiveHttpLogFilter)
            for log_filter in target_logger.filters
        ):
            target_logger.addFilter(_SensitiveHttpLogFilter())


_install_sensitive_http_log_filters()


class WeComIdentityError(Exception):
    """Raised when WeCom cannot confirm an internal employee identity."""


@dataclass(frozen=True)
class WeComSettings:
    corp_id: str
    agent_id: str
    secret: str


class WeComClient:
    def __init__(
        self, settings: WeComSettings, transport: httpx.BaseTransport | None = None
    ):
        self.settings = settings
        self.client = httpx.Client(timeout=10, transport=transport)

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "appid": self.settings.corp_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "snsapi_base",
                "state": state,
            }
        )
        return f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect"

    def user_id_from_code(self, code: str) -> str:
        try:
            response = self.client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
                params={"access_token": self._get_access_token(), "code": code},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise WeComIdentityError from None

        if not isinstance(payload, dict):
            raise WeComIdentityError
        user_id = payload.get("userid")
        if payload.get("errcode") != 0 or not user_id:
            raise WeComIdentityError
        return user_id

    def _get_access_token(self) -> str:
        with _TOKEN_CACHE_LOCK:
            cached_token = _TOKEN_CACHE.get(self.settings.corp_id)
            if cached_token and monotonic() < cached_token[1]:
                return cached_token[0]

        try:
            response = self.client.get(
                "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                params={"corpid": self.settings.corp_id, "corpsecret": self.settings.secret},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            raise WeComIdentityError from None

        token = payload.get("access_token") if isinstance(payload, dict) else None
        if payload.get("errcode") != 0 or not token:
            raise WeComIdentityError

        expires_in = payload.get("expires_in", 0)
        try:
            expires_at = monotonic() + max(int(expires_in) - 60, 0)
        except (TypeError, ValueError):
            expires_at = monotonic()
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[self.settings.corp_id] = (token, expires_at)
        return token
