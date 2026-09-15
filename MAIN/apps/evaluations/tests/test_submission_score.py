from copy import deepcopy
from decimal import Decimal

import pytest
from django.db import models

from apps.evaluations.services.scoring import (
    ScoringIntegrityError,
    calculate_submission_score,
)
from tests.factories import create_final_submission


pytestmark = pytest.mark.django_db


def test_submission_score_uses_frozen_item_weights(final_submission):
    assert calculate_submission_score(final_submission) == Decimal("4.20")


def test_submission_score_rounds_half_up_only_at_public_boundary(task):
    snapshot = deepcopy(task.project_subject.template_snapshot)
    snapshot["items"][0]["weight"] = "0.98750"
    snapshot["items"][1]["weight"] = "0.01250"
    task.project_subject.template_snapshot = snapshot
    task.project_subject.save(update_fields=["template_snapshot"])
    submission = create_final_submission(
        task,
        {
            snapshot["items"][0]["snapshot_item_id"]: 3,
            snapshot["items"][1]["snapshot_item_id"]: 1,
        },
    )

    assert calculate_submission_score(submission) == Decimal("2.98")


def test_submission_score_rejects_non_final_submission(task):
    submission = task.submissions.create(is_final=False)

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_submission_score(submission)

    assert raised.value.code == "SUBMISSION_NOT_FINAL"


def test_submission_score_requires_exact_answer_set(final_submission):
    final_submission.answers.order_by("pk").first().delete()

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_submission_score(final_submission)

    assert raised.value.code == "SUBMISSION_ANSWERS_INVALID"


def test_submission_score_rejects_score_outside_frozen_item_range(final_submission):
    project_subject = final_submission.task.project_subject
    snapshot = deepcopy(project_subject.template_snapshot)
    snapshot["items"][0]["score_max"] = 4
    project_subject.template_snapshot = snapshot
    project_subject.save(update_fields=["template_snapshot"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_submission_score(final_submission)

    assert raised.value.code == "SUBMISSION_SCORE_INVALID"


def test_submission_score_rejects_noncanonical_frozen_weight(final_submission):
    project_subject = final_submission.task.project_subject
    snapshot = deepcopy(project_subject.template_snapshot)
    snapshot["items"][0]["weight"] = "0.6000"
    project_subject.template_snapshot = snapshot
    project_subject.save(update_fields=["template_snapshot"])

    with pytest.raises(ScoringIntegrityError) as raised:
        calculate_submission_score(final_submission)

    assert raised.value.code == "FROZEN_TEMPLATE_SNAPSHOT_INVALID"


def test_submission_score_does_not_read_current_template_items(final_submission):
    template = final_submission.task.project_subject.template
    for item in template.items.all():
        item.weight = Decimal("0.50")
        item.score_max = 1
        models.Model.save(item, update_fields=["weight", "score_max"])

    assert calculate_submission_score(final_submission) == Decimal("4.20")
