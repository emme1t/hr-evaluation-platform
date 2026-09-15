from django.db.models import Exists, OuterRef

from .models import EvaluationTask, Submission


def tasks_for_user(user):
    return (
        EvaluationTask.objects.filter(evaluator__user=user, evaluator__is_active=True)
        .annotate(
            has_draft=Exists(
                Submission.objects.filter(
                    task_id=OuterRef("pk"), is_final=False
                )
            )
        )
        .select_related("project", "project_subject", "subject")
        .order_by(
            "project__deadline",
            "project__public_id",
            "subject__name",
            "public_id",
        )
    )
