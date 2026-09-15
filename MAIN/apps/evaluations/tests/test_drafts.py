import json
from uuid import UUID, uuid4

import pytest
from django.test import Client
from django.urls import resolve, reverse

from apps.evaluations.services.submissions import (
    StaleDraftError,
    SubmissionAccessError,
    SubmissionValidationError,
    save_draft,
)


@pytest.mark.django_db
def test_draft_replaces_all_answers_and_increments_version(
    task, evaluator_user, complete_answers
):
    first_item, second_item = complete_answers
    first = save_draft(task, evaluator_user, {first_item: 3})
    first_saved_at = first.saved_at

    replacement = save_draft(task, evaluator_user, {second_item: 4})

    assert replacement.pk == first.pk
    assert replacement.version == 2
    assert replacement.saved_at >= first_saved_at
    assert list(
        replacement.answers.values_list("item_snapshot_id", "score")
    ) == [(UUID(second_item), 4)]


@pytest.mark.django_db
def test_stale_expected_version_preserves_answers_version_and_saved_time(
    task, evaluator_user, complete_answers
):
    item_id = next(iter(complete_answers))
    current = save_draft(
        task, evaluator_user, {item_id: 2}, expected_version=0
    )
    saved_at = current.saved_at

    with pytest.raises(StaleDraftError) as raised:
        save_draft(task, evaluator_user, {item_id: 5}, expected_version=0)

    current.refresh_from_db()
    assert raised.value.code == "DRAFT_VERSION_STALE"
    assert raised.value.current_version == 1
    assert current.version == 1
    assert current.saved_at == saved_at
    assert current.answers.get().score == 2


@pytest.mark.django_db
@pytest.mark.parametrize("older_completes_first", [True, False])
def test_version_precondition_keeps_current_values_for_both_completion_orders(
    task, evaluator_user, complete_answers, older_completes_first
):
    item_id = next(iter(complete_answers))
    older = {item_id: 2}
    current = {item_id: 5}

    if older_completes_first:
        save_draft(task, evaluator_user, older, expected_version=0)
        with pytest.raises(StaleDraftError) as stale:
            save_draft(task, evaluator_user, current, expected_version=0)
        save_draft(
            task,
            evaluator_user,
            current,
            expected_version=stale.value.current_version,
        )
    else:
        save_draft(task, evaluator_user, current, expected_version=0)
        with pytest.raises(StaleDraftError):
            save_draft(task, evaluator_user, older, expected_version=0)

    draft = task.submissions.get(is_final=False)
    assert draft.answers.get().score == 5
    assert draft.version == (2 if older_completes_first else 1)


@pytest.mark.django_db
def test_partial_and_empty_drafts_are_allowed(task, evaluator_user, complete_answers):
    item_id = next(iter(complete_answers))

    draft = save_draft(task, evaluator_user, {item_id: 2})
    cleared = save_draft(task, evaluator_user, {})

    assert cleared.pk == draft.pk
    assert cleared.answers.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("answers", "code"),
    [
        ([("duplicate", 3), ("duplicate", 4)], "ANSWER_DUPLICATE"),
        ({str(uuid4()): 3}, "ANSWER_UNKNOWN"),
        ({}, None),
    ],
)
def test_draft_rejects_duplicate_or_unknown_keys(
    task, evaluator_user, complete_answers, answers, code
):
    if isinstance(answers, list):
        item_id = next(iter(complete_answers))
        answers = [(item_id, 3), (item_id, 4)]
    if code is None:
        save_draft(task, evaluator_user, answers)
        return

    with pytest.raises(SubmissionValidationError) as raised:
        save_draft(task, evaluator_user, answers)

    assert raised.value.code == code


@pytest.mark.django_db
def test_draft_service_enforces_ownership(task, other_task, evaluator_user):
    with pytest.raises(SubmissionAccessError) as raised:
        save_draft(other_task, evaluator_user, {})

    assert raised.value.code == "TASK_NOT_FOUND"


@pytest.mark.django_db
def test_draft_route_returns_saved_time_and_monotonic_version(
    client, task, evaluator_user, complete_answers
):
    url = reverse("evaluations:task-draft", args=[task.public_id])
    client.force_login(evaluator_user)

    first = client.post(
        url,
        data=json.dumps({"answers": complete_answers, "expected_version": 0}),
        content_type="application/json",
    )
    second = client.post(
        url,
        data=json.dumps({"answers": complete_answers, "expected_version": 1}),
        content_type="application/json",
    )

    assert reverse("evaluations:task-draft", args=[task.public_id]) == url
    assert resolve(url).view_name == "evaluations:task-draft"
    assert first.status_code == second.status_code == 200
    assert set(first.json()) == {"saved_at", "version"}
    assert second.json()["version"] > first.json()["version"]


@pytest.mark.django_db
@pytest.mark.parametrize("older_completes_first", [True, False])
def test_draft_http_conflict_keeps_current_values_for_both_completion_orders(
    client, task, evaluator_user, complete_answers, older_completes_first
):
    item_id = next(iter(complete_answers))
    url = reverse("evaluations:task-draft", args=[task.public_id])
    client.force_login(evaluator_user)

    def post(score, expected_version):
        return client.post(
            url,
            data=json.dumps(
                {
                    "answers": {item_id: score},
                    "expected_version": expected_version,
                }
            ),
            content_type="application/json",
        )

    if older_completes_first:
        assert post(2, 0).status_code == 200
        stale = post(5, 0)
        assert stale.status_code == 409
        assert stale.json() == {
            "error": {
                "code": "DRAFT_VERSION_STALE",
                "message": "草稿版本已更新",
            },
            "current_version": 1,
        }
        assert post(5, stale.json()["current_version"]).status_code == 200
    else:
        assert post(5, 0).status_code == 200
        stale = post(2, 0)
        assert stale.status_code == 409
        assert stale.json()["current_version"] == 1

    draft = task.submissions.get(is_final=False)
    assert draft.answers.get().score == 5


@pytest.mark.django_db
def test_owned_detail_exposes_current_draft_version_only_on_task_form(
    client, task, evaluator_user, complete_answers
):
    save_draft(task, evaluator_user, complete_answers, expected_version=0)
    client.force_login(evaluator_user)

    detail = client.get(reverse("evaluations:task_detail", args=[task.public_id]))
    task_list = client.get(reverse("evaluations:task_list"))

    assert 'data-draft-version="1"' in detail.content.decode()
    assert "data-draft-version" not in task_list.content.decode()


@pytest.mark.django_db
def test_draft_route_rejects_missing_csrf_token(task, evaluator_user, complete_answers):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(evaluator_user)
    detail = csrf_client.get(reverse("evaluations:task_detail", args=[task.public_id]))
    url = reverse("evaluations:task-draft", args=[task.public_id])

    rejected = csrf_client.post(
        url,
        data=json.dumps({"answers": complete_answers, "expected_version": 0}),
        content_type="application/json",
    )
    accepted = csrf_client.post(
        url,
        data=json.dumps({"answers": complete_answers, "expected_version": 0}),
        content_type="application/json",
        HTTP_X_CSRFTOKEN=detail.cookies["csrftoken"].value,
    )

    assert rejected.status_code == 403
    assert accepted.status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"{", "PAYLOAD_MALFORMED"),
        (json.dumps({"answers": []}).encode(), "ANSWERS_MALFORMED"),
        (
            b'{"answers":{"same":3,"same":4}}',
            "PAYLOAD_DUPLICATE_KEY",
        ),
    ],
)
def test_draft_route_returns_stable_errors_for_malformed_payloads(
    client, task, evaluator_user, body, expected_code
):
    client.force_login(evaluator_user)

    response = client.post(
        reverse("evaluations:task-draft", args=[task.public_id]),
        data=body,
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == expected_code


@pytest.mark.django_db
def test_draft_route_rejects_oversized_body_before_saving(
    client, task, evaluator_user
):
    client.force_login(evaluator_user)
    body = json.dumps({"answers": {}, "padding": "x" * 140_000})

    response = client.post(
        reverse("evaluations:task-draft", args=[task.public_id]),
        data=body,
        content_type="application/json",
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert task.submissions.count() == 0


@pytest.mark.django_db
def test_draft_route_rejects_too_many_answers_with_stable_domain_error(
    client, task, evaluator_user
):
    client.force_login(evaluator_user)
    answers = {str(uuid4()): 3 for _ in range(1001)}

    response = client.post(
        reverse("evaluations:task-draft", args=[task.public_id]),
        data=json.dumps({"answers": answers, "expected_version": 0}),
        content_type="application/json",
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "ANSWERS_TOO_LARGE"


@pytest.mark.django_db
def test_forwarded_and_missing_draft_routes_are_nondisclosing(
    client, evaluator_user, other_task
):
    client.force_login(evaluator_user)
    forwarded = client.post(
        reverse("evaluations:task-draft", args=[other_task.public_id]),
        data=b'{"answers":{}}',
        content_type="application/json",
    )
    missing = client.post(
        reverse("evaluations:task-draft", args=[uuid4()]),
        data=b'{"answers":{}}',
        content_type="application/json",
    )

    assert forwarded.status_code == missing.status_code == 404
    assert forwarded.content == missing.content


@pytest.mark.django_db
def test_saved_draft_appears_in_draft_bucket(
    client, task, evaluator_user, complete_answers
):
    save_draft(task, evaluator_user, complete_answers)
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_list"))

    groups = response.context["project_groups"]
    assert any(task.pk == draft.pk for group in groups for draft in group.buckets["draft"])
    assert task.subject.name in response.content.decode()


@pytest.mark.django_db
def test_saved_draft_scores_are_restored_only_on_owned_detail(
    client, task, evaluator_user, complete_answers
):
    item_id = next(iter(complete_answers))
    save_draft(task, evaluator_user, {item_id: 3})
    client.force_login(evaluator_user)

    detail = client.get(reverse("evaluations:task_detail", args=[task.public_id]))
    task_list = client.get(reverse("evaluations:task_list"))

    assert (
        f'data-snapshot-item-id="{item_id}" value="3" checked'
        in detail.content.decode()
    )
    assert f"score-{item_id}" not in task_list.content.decode()


@pytest.mark.django_db
def test_unauthenticated_draft_and_submit_redirect_without_reading_payload(
    client, task
):
    draft_url = reverse("evaluations:task-draft", args=[task.public_id])
    submit_url = reverse("evaluations:task-submit", args=[task.public_id])

    draft = client.post(draft_url, data=b"not-json", content_type="application/json")
    submit = client.post(submit_url, data={"idempotency_key": "not-authorized"})

    assert draft.status_code == submit.status_code == 302
    assert draft.url == f"/auth/wecom/start/?next={draft_url}"
    assert submit.url == f"/auth/wecom/start/?next={submit_url}"
