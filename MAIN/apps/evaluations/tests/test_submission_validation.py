from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import resolve, reverse
from django.utils import timezone

from apps.evaluations.models import EvaluationProject, EvaluationTask, ProjectSubject
from apps.evaluations.services.frozen_snapshots import MAX_TEMPLATE_SNAPSHOT_ITEMS
from apps.evaluations.services.submissions import (
    SubmissionAccessError,
    SubmissionStateError,
    SubmissionValidationError,
    save_draft,
    submit_task,
)


@pytest.mark.django_db
def test_final_submission_is_complete_atomic_and_marks_task_submitted(
    task, evaluator_user, complete_answers
):
    first_item = next(iter(complete_answers))
    draft = save_draft(task, evaluator_user, {first_item: 1})

    submission = submit_task(task, evaluator_user, complete_answers, "final-key-1")

    task.refresh_from_db()
    assert submission.pk == draft.pk
    assert submission.is_final is True
    assert submission.idempotency_key == "final-key-1"
    assert submission.submitted_at is not None
    assert task.status == EvaluationTask.Status.SUBMITTED
    assert {
        str(item_id): score
        for item_id, score in submission.answers.values_list(
            "item_snapshot_id", "score"
        )
    } == complete_answers


@pytest.mark.django_db
def test_submit_rejects_missing_required_answers_without_mutating_draft(
    task, evaluator_user, complete_answers
):
    first_item = next(iter(complete_answers))
    draft = save_draft(task, evaluator_user, {first_item: 3})

    with pytest.raises(SubmissionValidationError, match="请完成全部必填评价项") as raised:
        submit_task(task, evaluator_user, {first_item: 4}, "missing-key")

    draft.refresh_from_db()
    task.refresh_from_db()
    assert raised.value.code == "ANSWERS_INCOMPLETE"
    assert draft.is_final is False
    assert draft.answers.get().score == 3
    assert task.status == EvaluationTask.Status.PENDING


@pytest.mark.django_db
@pytest.mark.parametrize("score", [True, False, "5", 5.0, None, 0, 6])
def test_score_must_be_a_real_integer_from_one_to_five(
    task, evaluator_user, complete_answers, score
):
    answers = dict(complete_answers)
    answers[next(iter(answers))] = score

    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(task, evaluator_user, answers, "invalid-score-key")

    assert raised.value.code == "SCORE_INVALID"


@pytest.mark.django_db
def test_snapshot_item_score_bounds_are_enforced(
    task, evaluator_user, complete_answers
):
    snapshot = deepcopy(task.project_subject.template_snapshot)
    snapshot["items"][0]["score_min"] = 2
    snapshot["items"][0]["score_max"] = 4
    ProjectSubject.objects.filter(pk=task.project_subject_id).update(
        template_snapshot=snapshot
    )
    answers = dict(complete_answers)
    answers[snapshot["items"][0]["snapshot_item_id"]] = 1

    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(task, evaluator_user, answers, "bounded-score-key")

    assert raised.value.code == "SCORE_OUT_OF_RANGE"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("answer_builder", "code"),
    [
        (lambda answers: {**answers, str(uuid4()): 3}, "ANSWER_UNKNOWN"),
        (
            lambda answers: [*answers.items(), (next(iter(answers)), 3)],
            "ANSWER_DUPLICATE",
        ),
        (lambda _answers: [([], 3)], "ANSWER_UNKNOWN"),
    ],
)
def test_submit_rejects_unknown_and_duplicate_answer_keys(
    task, evaluator_user, complete_answers, answer_builder, code
):
    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(
            task,
            evaluator_user,
            answer_builder(dict(complete_answers)),
            "answer-key-error",
        )

    assert raised.value.code == code


@pytest.mark.django_db
def test_submit_uses_shared_frozen_snapshot_validation_contract(
    task, evaluator_user, complete_answers
):
    snapshot = deepcopy(task.project_subject.template_snapshot)
    snapshot["items"][1]["snapshot_item_id"] = snapshot["items"][0][
        "snapshot_item_id"
    ]
    ProjectSubject.objects.filter(pk=task.project_subject_id).update(
        template_snapshot=snapshot
    )

    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(task, evaluator_user, complete_answers, "bad-snapshot-key")

    assert raised.value.code == "FROZEN_TEMPLATE_SNAPSHOT_INVALID"


@pytest.mark.django_db
def test_service_rejects_other_or_inactive_evaluator(
    task, other_task, evaluator_user, complete_answers
):
    with pytest.raises(SubmissionAccessError) as other_error:
        submit_task(other_task, evaluator_user, complete_answers, "other-task-key")

    task.evaluator.is_active = False
    task.evaluator.save(update_fields=["is_active"])
    with pytest.raises(SubmissionAccessError) as inactive_error:
        submit_task(task, evaluator_user, complete_answers, "inactive-key")

    assert other_error.value.code == inactive_error.value.code == "TASK_NOT_FOUND"


@pytest.mark.django_db
@pytest.mark.parametrize("state", ["project", "deadline", "task"])
def test_service_rejects_inactive_project_deadline_or_task_status(
    task, evaluator_user, complete_answers, state
):
    if state == "project":
        task.project.status = EvaluationProject.Status.CLOSED
        task.project.save(update_fields=["status"])
        expected = "PROJECT_NOT_ACTIVE"
    elif state == "deadline":
        task.project.deadline = timezone.now() - timedelta(microseconds=1)
        task.project.save(update_fields=["deadline"])
        expected = "TASK_DEADLINE_PASSED"
    else:
        task.status = EvaluationTask.Status.NOT_SUBMITTED
        task.save(update_fields=["status"])
        expected = "TASK_NOT_EDITABLE"

    with pytest.raises(SubmissionStateError) as raised:
        submit_task(task, evaluator_user, complete_answers, f"state-{state}-key")

    assert raised.value.code == expected


@pytest.mark.django_db
def test_submit_route_redirects_to_read_only_confirmation_state(
    client, task, evaluator_user, complete_answers
):
    client.force_login(evaluator_user)
    url = reverse("evaluations:task-submit", args=[task.public_id])
    data = {f"score-{item_id}": score for item_id, score in complete_answers.items()}
    data["idempotency_key"] = "browser-final-key"

    response = client.post(url, data=data)
    detail = client.get(response.url)

    assert resolve(url).view_name == "evaluations:task-submit"
    assert response.status_code == 302
    assert response.url == reverse("evaluations:task_detail", args=[task.public_id])
    assert detail.status_code == 200
    content = detail.content.decode()
    assert "已提交" in content
    assert "disabled" in content
    assert "checked" in content


@pytest.mark.django_db
def test_submit_route_enforces_csrf(task, evaluator_user, complete_answers):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(evaluator_user)
    detail_url = reverse("evaluations:task_detail", args=[task.public_id])
    detail = csrf_client.get(detail_url)
    url = reverse("evaluations:task-submit", args=[task.public_id])
    data = {f"score-{item_id}": score for item_id, score in complete_answers.items()}
    data["idempotency_key"] = "csrf-final-key"

    rejected = csrf_client.post(url, data=data)
    data["csrfmiddlewaretoken"] = detail.cookies["csrftoken"].value
    accepted = csrf_client.post(url, data=data)

    assert rejected.status_code == 403
    assert accepted.status_code == 302


@pytest.mark.django_db
def test_submit_route_rejects_duplicate_form_answer_keys(
    client, task, evaluator_user, complete_answers
):
    client.force_login(evaluator_user)
    pairs = [("idempotency_key", "duplicate-form-key")]
    for item_id, score in complete_answers.items():
        pairs.append((f"score-{item_id}", str(score)))
    first_item = next(iter(complete_answers))
    pairs.append((f"score-{first_item}", "2"))

    response = client.post(
        reverse("evaluations:task-submit", args=[task.public_id]),
        data=urlencode(pairs),
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 400
    assert "评价项不能重复" in response.content.decode()
    task.refresh_from_db()
    assert task.status == EvaluationTask.Status.PENDING


@pytest.mark.django_db
def test_submit_route_rejects_oversized_body_before_submission(
    client, task, evaluator_user
):
    client.force_login(evaluator_user)

    response = client.post(
        reverse("evaluations:task-submit", args=[task.public_id]),
        data=urlencode(
            {
                "idempotency_key": "oversized-final-key",
                "padding": "x" * 140_000,
            }
        ),
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 413
    assert "请求内容过大" in response.content.decode()
    assert task.submissions.count() == 0


@pytest.mark.django_db
def test_submit_route_accepts_the_maximum_frozen_snapshot_item_count(
    client, task, evaluator_user
):
    snapshot = deepcopy(task.project_subject.template_snapshot)
    source = snapshot["items"][0]
    snapshot["items"] = [
        {
            **source,
            "snapshot_item_id": str(uuid4()),
            "order": index,
            "weight": "1.00000" if index == 0 else "0.00000",
        }
        for index in range(MAX_TEMPLATE_SNAPSHOT_ITEMS)
    ]
    ProjectSubject.objects.filter(pk=task.project_subject_id).update(
        template_snapshot=snapshot
    )
    data = {
        **{
            f"score-{item['snapshot_item_id']}": item["score_max"]
            for item in snapshot["items"]
        },
        "idempotency_key": "maximum-items-key",
    }
    client.force_login(evaluator_user)

    response = client.post(
        reverse("evaluations:task-submit", args=[task.public_id]),
        data=urlencode(data),
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 302
    assert task.submissions.get(is_final=True).answers.count() == 1000


@pytest.mark.django_db
def test_submit_route_translates_excess_form_fields_to_stable_resource_error(
    client, task, evaluator_user
):
    client.force_login(evaluator_user)
    pairs = [("idempotency_key", "too-many-form-fields")]
    pairs.extend((f"score-{uuid4()}", "3") for _ in range(1100))

    response = client.post(
        reverse("evaluations:task-submit", args=[task.public_id]),
        data=urlencode(pairs),
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 413
    assert "评价项数量过多" in response.content.decode()


@pytest.mark.django_db
def test_forwarded_and_missing_submit_routes_are_nondisclosing(
    client, evaluator_user, other_task
):
    client.force_login(evaluator_user)
    data = {"idempotency_key": "nondisclosing-key"}
    forwarded = client.post(
        reverse("evaluations:task-submit", args=[other_task.public_id]), data=data
    )
    missing = client.post(
        reverse("evaluations:task-submit", args=[uuid4()]), data=data
    )

    assert forwarded.status_code == missing.status_code == 404
    assert forwarded.content == missing.content


@pytest.mark.django_db
def test_task_form_has_server_submit_and_draft_contract_without_score_urls(
    client, task, evaluator_user
):
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_detail", args=[task.public_id]))

    content = response.content.decode()
    assert response.status_code == 200
    assert f'action="{reverse("evaluations:task-submit", args=[task.public_id])}"' in content
    assert f'data-draft-url="{reverse("evaluations:task-draft", args=[task.public_id])}"' in content
    assert "draft.js" in content
    assert "localStorage" not in content
    assert "sessionStorage" not in content
