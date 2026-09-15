from copy import deepcopy
import logging
from uuid import uuid4

import pytest
from django.urls import reverse

from apps.evaluations.models import ProjectSubject
from apps.evaluations.services.frozen_snapshots import (
    FrozenTemplateSnapshotValidationError,
    normalize_frozen_template_snapshot,
)
from apps.evaluations.services.projects import (
    ProjectStateError,
    validate_ready_project_integrity,
)


def _duplicate_item_id(snapshot):
    snapshot["items"][1]["snapshot_item_id"] = snapshot["items"][0]["snapshot_item_id"]
    return snapshot


def _exponent_weight(snapshot):
    snapshot["items"][0]["weight"] = "5e-1"
    return snapshot


def _invalid_total_weight(snapshot):
    snapshot["items"][0]["weight"] = "0.40000"
    return snapshot


def _too_many_items(snapshot):
    source = deepcopy(snapshot["items"][0])
    items = []
    for order in range(1001):
        item = deepcopy(source)
        item["snapshot_item_id"] = str(uuid4())
        item["order"] = order
        item["weight"] = "1.00000" if order == 0 else "0.00000"
        items.append(item)
    snapshot["items"] = items
    return snapshot


SNAPSHOT_CASES = (
    ("valid", lambda snapshot: snapshot, True),
    ("duplicate-item-id", _duplicate_item_id, False),
    ("exponent-weight", _exponent_weight, False),
    ("invalid-total-weight", _invalid_total_weight, False),
    ("too-many-items", _too_many_items, False),
)


@pytest.mark.django_db
@pytest.mark.parametrize(("_name", "mutate", "valid"), SNAPSHOT_CASES)
def test_frozen_template_snapshot_contract_has_one_normalized_boundary(
    own_task, _name, mutate, valid
):
    snapshot = mutate(deepcopy(own_task.project_subject.template_snapshot))

    if valid:
        normalized = normalize_frozen_template_snapshot(snapshot)
        assert normalized.items[0].title == snapshot["items"][0]["title"]
    else:
        with pytest.raises(FrozenTemplateSnapshotValidationError):
            normalize_frozen_template_snapshot(snapshot)


@pytest.mark.django_db
@pytest.mark.parametrize(("_name", "mutate", "valid"), SNAPSHOT_CASES)
def test_project_ready_and_evaluator_detail_share_frozen_snapshot_contract(
    client, caplog, evaluator_user, own_task, _name, mutate, valid
):
    snapshot = mutate(deepcopy(own_task.project_subject.template_snapshot))
    ProjectSubject.objects.filter(pk=own_task.project_subject_id).update(
        template_snapshot=snapshot
    )
    client.force_login(evaluator_user)
    caplog.set_level(logging.WARNING, logger="apps.evaluations.views.evaluator")

    response = client.get(
        reverse("evaluations:task_detail", args=[own_task.public_id])
    )

    if valid:
        assert response.status_code == 200
        assert (
            validate_ready_project_integrity(
                own_task.project,
                own_task.project.subjects.order_by("pk"),
                own_task.project.tasks.order_by("pk"),
            )
            == own_task.project.tasks.count()
        )
    else:
        assert response.status_code == 409
        content = response.content.decode()
        assert snapshot["name"] not in content
        assert snapshot["items"][0]["title"] not in content
        assert all(
            snapshot["name"] not in record.getMessage()
            and snapshot["items"][0]["title"] not in record.getMessage()
            for record in caplog.records
        )
        with pytest.raises(ProjectStateError) as raised:
            validate_ready_project_integrity(
                own_task.project,
                own_task.project.subjects.order_by("pk"),
                own_task.project.tasks.order_by("pk"),
            )
        assert raised.value.code == "PROJECT_FROZEN_DATA_CORRUPT"
