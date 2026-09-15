import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID


TEMPLATE_SNAPSHOT_KEYS = {"public_id", "name", "version", "category", "items"}
CATEGORY_SNAPSHOT_KEYS = {"public_id", "name"}
TEMPLATE_ITEM_SNAPSHOT_KEYS = {
    "snapshot_item_id",
    "group",
    "title",
    "order",
    "weight",
    "score_min",
    "score_max",
    "excellent_description",
    "good_description",
    "qualified_description",
    "improvement_description",
}
TEMPLATE_WEIGHT_PATTERN = re.compile(r"(?:0\.\d{5}|1\.00000)\Z")
MAX_TEMPLATE_SNAPSHOT_ITEMS = 1000


class FrozenTemplateSnapshotValidationError(ValueError):
    code = "FROZEN_TEMPLATE_SNAPSHOT_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class FrozenTemplateSnapshotItem:
    snapshot_item_id: str
    group: str
    title: str
    order: int
    weight: str
    score_min: int
    score_max: int
    excellent_description: str
    good_description: str
    qualified_description: str
    improvement_description: str


@dataclass(frozen=True)
class FrozenTemplateSnapshot:
    public_id: str
    name: str
    version: int
    category_public_id: str
    category_name: str
    items: tuple[FrozenTemplateSnapshotItem, ...]


def normalize_frozen_template_snapshot(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != TEMPLATE_SNAPSHOT_KEYS:
        raise FrozenTemplateSnapshotValidationError()
    if (
        not _is_canonical_uuid(snapshot.get("public_id"))
        or not _is_nonempty_string(snapshot.get("name"))
        or isinstance(snapshot.get("version"), bool)
        or not isinstance(snapshot.get("version"), int)
        or snapshot["version"] < 1
    ):
        raise FrozenTemplateSnapshotValidationError()

    category = snapshot.get("category")
    if (
        not isinstance(category, dict)
        or set(category) != CATEGORY_SNAPSHOT_KEYS
        or not _is_canonical_uuid(category.get("public_id"))
        or not _is_nonempty_string(category.get("name"))
    ):
        raise FrozenTemplateSnapshotValidationError()

    items = snapshot.get("items")
    if (
        not isinstance(items, list)
        or not items
        or len(items) > MAX_TEMPLATE_SNAPSHOT_ITEMS
    ):
        raise FrozenTemplateSnapshotValidationError()

    normalized_items = []
    item_ids = set()
    orders = set()
    total_weight = Decimal("0")
    for item in items:
        normalized_item, weight = _normalize_item(item, item_ids, orders)
        normalized_items.append(normalized_item)
        total_weight += weight

    if total_weight != Decimal("1.00000"):
        raise FrozenTemplateSnapshotValidationError()

    return FrozenTemplateSnapshot(
        public_id=snapshot["public_id"],
        name=snapshot["name"],
        version=snapshot["version"],
        category_public_id=category["public_id"],
        category_name=category["name"],
        items=tuple(sorted(normalized_items, key=lambda item: item.order)),
    )


def _normalize_item(item, item_ids, orders):
    if not isinstance(item, dict) or set(item) != TEMPLATE_ITEM_SNAPSHOT_KEYS:
        raise FrozenTemplateSnapshotValidationError()

    snapshot_item_id = item.get("snapshot_item_id")
    order = item.get("order")
    if (
        not _is_canonical_uuid(snapshot_item_id)
        or snapshot_item_id in item_ids
        or isinstance(order, bool)
        or not isinstance(order, int)
        or order < 0
        or order in orders
    ):
        raise FrozenTemplateSnapshotValidationError()
    item_ids.add(snapshot_item_id)
    orders.add(order)

    weight_value = item.get("weight")
    if (
        not isinstance(weight_value, str)
        or TEMPLATE_WEIGHT_PATTERN.fullmatch(weight_value) is None
    ):
        raise FrozenTemplateSnapshotValidationError()
    try:
        weight = Decimal(weight_value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise FrozenTemplateSnapshotValidationError() from exc
    if (
        not weight.is_finite()
        or weight < 0
        or format(weight, ".5f") != weight_value
    ):
        raise FrozenTemplateSnapshotValidationError()

    score_min = item.get("score_min")
    score_max = item.get("score_max")
    if (
        isinstance(score_min, bool)
        or isinstance(score_max, bool)
        or not isinstance(score_min, int)
        or not isinstance(score_max, int)
        or not 1 <= score_min <= score_max <= 5
    ):
        raise FrozenTemplateSnapshotValidationError()
    if not all(
        _is_nonempty_string(item.get(field))
        for field in (
            "group",
            "title",
            "excellent_description",
            "good_description",
            "qualified_description",
            "improvement_description",
        )
    ):
        raise FrozenTemplateSnapshotValidationError()

    return (
        FrozenTemplateSnapshotItem(
            snapshot_item_id=snapshot_item_id,
            group=item["group"],
            title=item["title"],
            order=order,
            weight=weight_value,
            score_min=score_min,
            score_max=score_max,
            excellent_description=item["excellent_description"],
            good_description=item["good_description"],
            qualified_description=item["qualified_description"],
            improvement_description=item["improvement_description"],
        ),
        weight,
    )


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _is_nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())
