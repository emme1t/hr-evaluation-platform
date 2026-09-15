"""Repository-wide row-lock order for shared mutable roster dependencies.

Project preparation, roster imports, and standalone FK mutations must acquire
overlapping shared rows in this order. Employee updates therefore precede any
category FK lock, while relationship upsert/FK updates lock employees before
relationships. Single-model deactivation paths take only their own row lock.
Project-local and import-batch rows may be locked first because they are not
shared between workflows. Sealed template versions are immutable and are
intentionally outside this order.
"""

from .models import Employee, EmployeeCategory, EvaluationRelationship


SHARED_MUTABLE_DEPENDENCY_LOCK_ORDER = (
    Employee,
    EmployeeCategory,
    EvaluationRelationship,
)


def lock_employee_rows(employee_ids):
    """Lock an employee set in the repository-wide first dependency slot."""
    ids = sorted(set(employee_ids))
    return list(
        Employee.objects.select_for_update(of=("self",))
        .filter(pk__in=ids)
        .order_by("pk")
    )
