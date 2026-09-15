import json

from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent


MAX_SUBMISSION_BODY_BYTES = 128 * 1024


class SubmissionPayloadError(ValueError):
    def __init__(self, message, code, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def parse_draft_request(request):
    body = bounded_request_body(request)
    if request.content_type != "application/json":
        raise SubmissionPayloadError(
            "请求内容格式无效", "PAYLOAD_MALFORMED"
        )
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
    except SubmissionPayloadError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubmissionPayloadError(
            "请求内容格式无效", "PAYLOAD_MALFORMED"
        ) from exc
    if not isinstance(payload, dict) or "answers" not in payload:
        raise SubmissionPayloadError(
            "请求内容格式无效", "PAYLOAD_MALFORMED"
        )
    if not isinstance(payload["answers"], dict):
        raise SubmissionPayloadError(
            "评价内容格式无效", "ANSWERS_MALFORMED"
        )
    if set(payload) != {"answers", "expected_version"}:
        raise SubmissionPayloadError(
            "请求内容格式无效", "PAYLOAD_MALFORMED"
        )
    expected_version = payload["expected_version"]
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 0
    ):
        raise SubmissionPayloadError(
            "草稿版本无效", "DRAFT_VERSION_INVALID"
        )
    return payload["answers"], expected_version


def parse_submit_request(request):
    bounded_request_body(request)
    allowed_plain_fields = {"csrfmiddlewaretoken", "idempotency_key"}
    answers = []
    key = None
    try:
        post_data = request.POST
    except TooManyFieldsSent as exc:
        raise SubmissionPayloadError(
            "评价项数量过多", "ANSWERS_TOO_LARGE", status=413
        ) from exc
    for field, values in post_data.lists():
        if field in allowed_plain_fields:
            if field == "idempotency_key":
                if len(values) != 1:
                    raise SubmissionPayloadError(
                        "提交标识无效", "IDEMPOTENCY_KEY_INVALID"
                    )
                key = values[0]
            continue
        if not field.startswith("score-"):
            raise SubmissionPayloadError(
                "请求内容格式无效", "PAYLOAD_MALFORMED"
            )
        item_id = field.removeprefix("score-")
        if len(values) != 1:
            answers.extend((item_id, _form_score(value)) for value in values)
        else:
            answers.append((item_id, _form_score(values[0])))
    return answers, key


def bounded_request_body(request):
    raw_length = request.META.get("CONTENT_LENGTH")
    try:
        content_length = int(raw_length) if raw_length not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise SubmissionPayloadError(
            "请求内容格式无效", "PAYLOAD_MALFORMED"
        ) from exc
    if content_length is not None and (
        content_length < 0 or content_length > MAX_SUBMISSION_BODY_BYTES
    ):
        raise SubmissionPayloadError(
            "请求内容过大", "PAYLOAD_TOO_LARGE", status=413
        )
    if getattr(request, "_read_started", False) and not hasattr(request, "_body"):
        return b""
    try:
        body = request.body
    except RequestDataTooBig as exc:
        raise SubmissionPayloadError(
            "请求内容过大", "PAYLOAD_TOO_LARGE", status=413
        ) from exc
    if len(body) > MAX_SUBMISSION_BODY_BYTES:
        raise SubmissionPayloadError(
            "请求内容过大", "PAYLOAD_TOO_LARGE", status=413
        )
    return body


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SubmissionPayloadError(
                "请求字段不能重复", "PAYLOAD_DUPLICATE_KEY"
            )
        result[key] = value
    return result


def _form_score(value):
    if isinstance(value, str) and value in {"1", "2", "3", "4", "5"}:
        return int(value)
    return value
