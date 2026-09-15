from decimal import Decimal

import pytest

from apps.evaluations.services.scoring import calculate_subject_result
from apps.roster.models import EvaluationRelationship


pytestmark = pytest.mark.django_db


def test_complete_result_averages_groups_before_503020_weighting(
    complete_subject_result_data,
):
    outcome = calculate_subject_result(**complete_subject_result_data)

    assert outcome.group_scores == {
        "manager": Decimal("4.00"),
        "same_department": Decimal("3.50"),
        "cross_department": Decimal("3.00"),
    }
    assert outcome.total_score == Decimal("3.65")
    assert outcome.status == "complete"
    assert outcome.missing_groups == ()
    assert outcome.valid_submission_count == 4


def test_aggregate_outcome_collections_are_immutable(complete_subject_result_data):
    outcome = calculate_subject_result(**complete_subject_result_data)

    with pytest.raises(TypeError):
        outcome.group_scores["manager"] = Decimal("1.00")
    with pytest.raises(AttributeError):
        outcome.missing_groups.append("manager")


def test_aggregate_does_not_read_current_relationship_rows(
    complete_subject_result_data,
):
    EvaluationRelationship.objects.filter(
        subject=complete_subject_result_data["subject"]
    ).update(is_active=False)

    outcome = calculate_subject_result(**complete_subject_result_data)

    assert outcome.total_score == Decimal("3.65")
    assert outcome.valid_submission_count == 4


def test_repeating_group_mean_keeps_precision_until_public_rounding(
    subject_result_builder,
):
    data = subject_result_builder(
        {
            "manager": ((5, 5), (4, 4), (1, 1)),
            "same_department": ((4, 4),),
            "cross_department": ((2, 2),),
        }
    )

    outcome = calculate_subject_result(**data)

    assert outcome.group_scores["manager"] == Decimal("3.33")
    assert outcome.total_score == Decimal("3.27")


def test_total_score_uses_half_up_at_point_zero_zero_five(subject_result_builder):
    data = subject_result_builder(
        {
            "manager": ((4, 4),),
            "same_department": ((3, 3),),
            "cross_department": ((3, 1),),
        },
        item_weights=("0.98750", "0.01250"),
    )

    outcome = calculate_subject_result(**data)

    assert outcome.group_scores["cross_department"] == Decimal("2.98")
    assert outcome.total_score == Decimal("3.50")


def test_submission_scores_are_not_quantized_before_group_mean(
    subject_result_builder,
):
    data = subject_result_builder(
        {
            # Raw manager scores: 3.004 and 3.008; raw mean: 3.006.
            "manager": ((3, 4), (3, 5)),
            "same_department": ((3, 3),),
            # Raw cross score: 2.012.
            "cross_department": ((2, 5),),
        },
        item_weights=("0.99600", "0.00400"),
    )

    outcome = calculate_subject_result(**data)

    # 3.006*0.50 + 3*0.30 + 2.012*0.20 = 2.8054 -> 2.81.
    # Rounding each submission first gives 2.8045 -> 2.80.
    assert outcome.group_scores["manager"] == Decimal("3.01")
    assert outcome.total_score == Decimal("2.81")


def test_group_mean_is_not_quantized_before_relationship_weighting(
    subject_result_builder,
):
    data = subject_result_builder(
        {
            # Raw manager scores: 3.004 and 3.008; raw mean: 3.006.
            "manager": ((3, 4), (3, 5)),
            "same_department": ((3, 3),),
            # Raw cross score: 2.004.
            "cross_department": ((2, 3),),
        },
        item_weights=("0.99600", "0.00400"),
    )

    outcome = calculate_subject_result(**data)

    # 3.006*0.50 + 3*0.30 + 2.004*0.20 = 2.8038 -> 2.80.
    # Rounding the manager mean to 3.01 first gives 2.8058 -> 2.81.
    assert outcome.group_scores["manager"] == Decimal("3.01")
    assert outcome.total_score == Decimal("2.80")
