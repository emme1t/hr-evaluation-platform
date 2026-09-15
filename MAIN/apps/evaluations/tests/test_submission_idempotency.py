from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import IntegrityError, close_old_connections, connection, transaction

from apps.evaluations.models import EvaluationTask, ProjectSubject, Submission
from apps.evaluations.services.frozen_snapshots import MAX_TEMPLATE_SNAPSHOT_ITEMS
from apps.evaluations.services import submissions as submission_service
from apps.evaluations.services.submissions import (
    SubmissionAccessError,
    SubmissionIdempotencyConflict,
    SubmissionStateError,
    SubmissionValidationError,
    submit_task,
)


@pytest.mark.django_db(transaction=True)
def test_same_idempotency_key_and_payload_returns_single_submission(
    task, evaluator_user, complete_answers
):
    first = submit_task(task, evaluator_user, complete_answers, "stable-key")
    second = submit_task(task, evaluator_user, complete_answers, "stable-key")

    assert first.pk == second.pk
    assert task.submissions.filter(is_final=True).count() == 1


@pytest.mark.django_db
def test_same_key_same_payload_replays_without_revalidating_corrupt_snapshot(
    task, evaluator_user, complete_answers
):
    first = submit_task(task, evaluator_user, complete_answers, "stable-key")
    ProjectSubject.objects.filter(pk=task.project_subject_id).update(
        template_snapshot={}
    )

    replay = submit_task(task, evaluator_user, complete_answers, "stable-key")

    assert replay.pk == first.pk
    assert task.submissions.filter(is_final=True).count() == 1


@pytest.mark.django_db
def test_same_idempotency_key_with_different_payload_is_a_conflict(
    task, evaluator_user, complete_answers
):
    submit_task(task, evaluator_user, complete_answers, "stable-key")
    changed = dict(complete_answers)
    first_item = next(iter(changed))
    changed[first_item] = 1 if changed[first_item] != 1 else 2

    with pytest.raises(SubmissionIdempotencyConflict) as raised:
        submit_task(task, evaluator_user, changed, "stable-key")

    assert raised.value.code == "IDEMPOTENCY_PAYLOAD_CONFLICT"


@pytest.mark.django_db
def test_different_key_after_final_returns_stable_submitted_error(
    task, evaluator_user, complete_answers
):
    submit_task(task, evaluator_user, complete_answers, "first-key")

    with pytest.raises(SubmissionStateError, match="该任务已提交") as raised:
        submit_task(task, evaluator_user, complete_answers, "different-key")

    assert raised.value.code == "TASK_ALREADY_SUBMITTED"


class FiniteOversizedAnswers:
    def __init__(self):
        self.iterated = 0

    def __iter__(self):
        for index in range(1001):
            self.iterated += 1
            yield (f"unknown-{index}", 3)


class LyingOversizedMapping(Mapping):
    def __init__(self):
        self.iterated = 0

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def items(self):
        for index in range(MAX_TEMPLATE_SNAPSHOT_ITEMS + 2):
            self.iterated += 1
            if self.iterated > MAX_TEMPLATE_SNAPSHOT_ITEMS + 1:
                raise AssertionError("mapping items were read beyond the bound")
            yield (f"unknown-{index}", 3)


@pytest.mark.django_db
@pytest.mark.parametrize("answers", [{}, None])
def test_different_key_after_final_precedes_incomplete_or_malformed_answers(
    task, evaluator_user, complete_answers, answers
):
    submit_task(task, evaluator_user, complete_answers, "first-key")

    with pytest.raises(SubmissionStateError, match="该任务已提交") as raised:
        submit_task(task, evaluator_user, answers, "different-key")

    assert raised.value.code == "TASK_ALREADY_SUBMITTED"


@pytest.mark.django_db
def test_different_key_after_final_does_not_iterate_oversized_answers(
    task, evaluator_user, complete_answers
):
    submit_task(task, evaluator_user, complete_answers, "first-key")
    oversized = FiniteOversizedAnswers()

    with pytest.raises(SubmissionStateError, match="该任务已提交") as raised:
        submit_task(task, evaluator_user, oversized, "different-key")

    assert raised.value.code == "TASK_ALREADY_SUBMITTED"
    assert oversized.iterated == 0


@pytest.mark.django_db
@pytest.mark.parametrize("answers", [{}, None])
def test_same_key_malformed_or_incomplete_payload_is_idempotency_conflict(
    task, evaluator_user, complete_answers, answers
):
    submission = submit_task(task, evaluator_user, complete_answers, "stable-key")

    with pytest.raises(SubmissionIdempotencyConflict) as raised:
        submit_task(task, evaluator_user, answers, "stable-key")

    assert raised.value.code == "IDEMPOTENCY_PAYLOAD_CONFLICT"
    assert task.submissions.get(is_final=True).pk == submission.pk


@pytest.mark.django_db
def test_same_key_oversized_payload_is_idempotency_conflict_with_bounded_read(
    task, evaluator_user, complete_answers
):
    submission = submit_task(task, evaluator_user, complete_answers, "stable-key")
    oversized = FiniteOversizedAnswers()

    with pytest.raises(SubmissionIdempotencyConflict) as raised:
        submit_task(task, evaluator_user, oversized, "stable-key")

    assert raised.value.code == "IDEMPOTENCY_PAYLOAD_CONFLICT"
    assert oversized.iterated == 1001
    assert task.submissions.get(is_final=True).pk == submission.pk


@pytest.mark.django_db
def test_ownership_gate_precedes_existing_final_and_payload_inspection(
    other_task, evaluator_user
):
    Submission.objects.create(
        task=other_task,
        is_final=True,
        idempotency_key="private-final-key",
    )
    payload = FiniteOversizedAnswers()

    with pytest.raises(SubmissionAccessError) as raised:
        submit_task(other_task, evaluator_user, payload, "different-key")

    assert raised.value.code == "TASK_NOT_FOUND"
    assert payload.iterated == 0


@pytest.mark.django_db
@pytest.mark.parametrize("key", [None, "", " ", "x" * 129, "包含空格"])
def test_idempotency_key_is_explicit_and_bounded(
    task, evaluator_user, complete_answers, key
):
    with pytest.raises(SubmissionIdempotencyConflict) as raised:
        submit_task(task, evaluator_user, complete_answers, key)

    assert raised.value.code == "IDEMPOTENCY_KEY_INVALID"


@pytest.mark.django_db
def test_service_rejects_answer_iterables_over_the_snapshot_resource_limit(
    task, evaluator_user
):
    item_id = task.project_subject.template_snapshot["items"][0][
        "snapshot_item_id"
    ]
    oversized = ((item_id, 3) for _ in range(1001))

    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(task, evaluator_user, oversized, "oversized-answer-key")

    assert raised.value.code == "ANSWERS_TOO_LARGE"


@pytest.mark.django_db
def test_mapping_len_is_not_trusted_and_items_stop_at_limit_plus_one(
    task, evaluator_user
):
    answers = LyingOversizedMapping()

    with pytest.raises(SubmissionValidationError) as raised:
        submit_task(task, evaluator_user, answers, "lying-mapping-key")

    assert raised.value.code == "ANSWERS_TOO_LARGE"
    assert answers.iterated == MAX_TEMPLATE_SNAPSHOT_ITEMS + 1


@pytest.mark.django_db(transaction=True)
def test_database_enforces_one_final_and_unique_non_null_task_key(task):
    Submission.objects.create(task=task, is_final=True, idempotency_key="db-key-1")

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Submission.objects.create(
                task=task, is_final=True, idempotency_key="db-key-2"
            )

    task.status = EvaluationTask.Status.PENDING
    task.save(update_fields=["status"])
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Submission.objects.create(
                task=task, is_final=False, idempotency_key="db-key-1"
            )


@pytest.mark.django_db
def test_unrelated_integrity_error_is_not_translated_or_swallowed(
    task, evaluator_user, complete_answers, monkeypatch
):
    unrelated = IntegrityError("unrelated database failure")

    def fail_answer_write(_submission, _answers):
        raise unrelated

    monkeypatch.setattr(submission_service, "_replace_answers", fail_answer_write)

    with pytest.raises(IntegrityError) as raised:
        submit_task(task, evaluator_user, complete_answers, "unrelated-error-key")

    assert raised.value is unrelated
    assert task.submissions.count() == 0


def _concurrent_submit(task_id, user_id, answers, key, barrier):
    close_old_connections()
    try:
        from django.contrib.auth import get_user_model

        task = EvaluationTask.objects.get(pk=task_id)
        user = get_user_model().objects.get(pk=user_id)
        barrier.wait(timeout=10)
        try:
            submission = submit_task(task, user, answers, key)
            return ("ok", submission.pk)
        except SubmissionStateError as exc:
            return (exc.code, None)
    finally:
        close_old_connections()


@pytest.mark.django_db(transaction=True)
def test_simultaneous_final_submissions_are_serialized_on_postgresql(
    task, evaluator_user, complete_answers
):
    if connection.vendor != "postgresql":
        pytest.skip("requires PostgreSQL row locking and independent connections")
    barrier = Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                _concurrent_submit,
                task.pk,
                evaluator_user.pk,
                complete_answers,
                key,
                barrier,
            )
            for key in ("concurrent-key-a", "concurrent-key-b")
        ]
        results = [future.result(timeout=20) for future in futures]

    assert sorted(result[0] for result in results) == [
        "TASK_ALREADY_SUBMITTED",
        "ok",
    ]
    assert Submission.objects.filter(task=task, is_final=True).count() == 1
