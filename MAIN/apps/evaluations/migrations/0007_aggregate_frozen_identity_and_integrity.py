from itertools import combinations
from uuid import UUID

import django.db.models.deletion
from django.db import migrations, models


GROUP_FIELDS = (
    ("manager", "manager_score"),
    ("same_department", "same_department_score"),
    ("cross_department", "cross_department_score"),
)


def status_and_missing_groups_condition():
    condition = models.Q(
        status="complete",
        total_score__isnull=False,
        manager_score__isnull=False,
        same_department_score__isnull=False,
        cross_department_score__isnull=False,
        missing_groups=[],
    )
    for size in range(1, len(GROUP_FIELDS) + 1):
        for missing in combinations(GROUP_FIELDS, size):
            missing_names = [name for name, _ in missing]
            missing_fields = {field for _, field in missing}
            condition |= models.Q(
                status="incomplete",
                total_score__isnull=True,
                missing_groups=missing_names,
                **{
                    f"{field}__isnull": field in missing_fields
                    for _, field in GROUP_FIELDS
                },
            )
    return condition


def populate_frozen_subject_identity(apps, schema_editor):
    AggregateResult = apps.get_model("evaluations", "AggregateResult")
    ProjectSubject = apps.get_model("evaluations", "ProjectSubject")
    database = schema_editor.connection.alias
    for result in AggregateResult.objects.using(database).iterator():
        try:
            project_subject = (
                ProjectSubject.objects.using(database)
                .select_related("subject")
                .get(
                    project_id=result.project_id,
                    subject_id=result.subject_id,
                )
            )
            snapshot = project_subject.subject_snapshot
            frozen_public_id = UUID(snapshot["public_id"])
            frozen_name = snapshot["name"]
        except (
            ProjectSubject.DoesNotExist,
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            raise RuntimeError(
                "Cannot migrate aggregate result with invalid frozen subject identity"
            ) from exc
        if frozen_public_id != project_subject.subject.public_id:
            raise RuntimeError(
                "Cannot migrate aggregate result with invalid frozen subject identity"
            )
        if not isinstance(frozen_name, str) or not frozen_name.strip():
            raise RuntimeError(
                "Cannot migrate aggregate result with blank frozen subject name"
            )
        AggregateResult.objects.using(database).filter(pk=result.pk).update(
            project_subject_id=project_subject.pk,
            frozen_subject_public_id=frozen_public_id,
            frozen_subject_name=frozen_name,
        )


def clear_frozen_subject_identity(apps, schema_editor):
    AggregateResult = apps.get_model("evaluations", "AggregateResult")
    AggregateResult.objects.using(schema_editor.connection.alias).update(
        project_subject_id=None,
        frozen_subject_public_id=None,
        frozen_subject_name=None,
    )


class Migration(migrations.Migration):
    dependencies = [("evaluations", "0006_aggregateresult")]

    operations = [
        migrations.AddField(
            model_name="aggregateresult",
            name="project_subject",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="aggregate_results",
                to="evaluations.projectsubject",
            ),
        ),
        migrations.AddField(
            model_name="aggregateresult",
            name="frozen_subject_public_id",
            field=models.UUIDField(null=True),
        ),
        migrations.AddField(
            model_name="aggregateresult",
            name="frozen_subject_name",
            field=models.CharField(max_length=200, null=True),
        ),
        migrations.RunPython(
            populate_frozen_subject_identity,
            clear_frozen_subject_identity,
        ),
        migrations.AlterField(
            model_name="aggregateresult",
            name="project_subject",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="aggregate_results",
                to="evaluations.projectsubject",
            ),
        ),
        migrations.AlterField(
            model_name="aggregateresult",
            name="frozen_subject_public_id",
            field=models.UUIDField(),
        ),
        migrations.AlterField(
            model_name="aggregateresult",
            name="frozen_subject_name",
            field=models.CharField(max_length=200),
        ),
        migrations.RemoveConstraint(
            model_name="aggregateresult",
            name="unique_project_subject_algorithm_result",
        ),
        migrations.RemoveConstraint(
            model_name="aggregateresult",
            name="aggregate_status_matches_total",
        ),
        migrations.AlterModelOptions(
            name="aggregateresult",
            options={
                "ordering": [
                    "project_id",
                    "project_subject_id",
                    "algorithm_version",
                ]
            },
        ),
        migrations.AddConstraint(
            model_name="aggregateresult",
            constraint=models.UniqueConstraint(
                fields=("project_subject", "algorithm_version"),
                name="unique_frozen_subject_algorithm_result",
            ),
        ),
        migrations.AddConstraint(
            model_name="aggregateresult",
            constraint=models.CheckConstraint(
                condition=status_and_missing_groups_condition(),
                name="aggregate_status_missing_groups_match",
            ),
        ),
        migrations.AddConstraint(
            model_name="aggregateresult",
            constraint=models.CheckConstraint(
                condition=models.Q(algorithm_version__regex=r".*\S.*"),
                name="aggregate_algorithm_version_nonempty",
            ),
        ),
        migrations.AddConstraint(
            model_name="aggregateresult",
            constraint=models.CheckConstraint(
                condition=models.Q(input_fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="aggregate_fingerprint_lower_hex",
            ),
        ),
        migrations.AddConstraint(
            model_name="aggregateresult",
            constraint=models.CheckConstraint(
                condition=models.Q(frozen_subject_name__regex=r".*\S.*"),
                name="aggregate_frozen_subject_name_nonempty",
            ),
        ),
    ]
