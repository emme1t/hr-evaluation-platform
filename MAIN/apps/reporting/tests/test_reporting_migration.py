from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.recorder import MigrationRecorder

from apps.evaluations.models import EvaluationProject
from apps.reporting.services.issues import export_issue_workbook
from apps.reporting.services.raw import RawExportError, export_raw_zip
from apps.reporting.services.summary import TemplateStructureError, export_summary_workbook


ALREADY_RECORDED_TARGET = (
    "evaluations",
    "0008_evaluationproject_summary_template_and_more",
)
PROJECT_REPORTING_SNAPSHOT_TARGET = (
    "evaluations",
    "0009_evaluationproject_reporting_snapshot",
)


@pytest.mark.parametrize("status", ["ready", "active"])
@pytest.mark.django_db(transaction=True)
def test_projects_at_recorded_0008_gain_project_snapshot_without_breaking_scoring(
    status, project_results, hr_admin
):
    project_pk = project_results.project.pk
    project_public_id = project_results.project.public_id
    EvaluationProject.objects.filter(pk=project_pk).update(status=status)
    current_leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()

    try:
        MigrationExecutor(connection).migrate([ALREADY_RECORDED_TARGET])
        recorded = MigrationRecorder(connection).applied_migrations()
        assert ALREADY_RECORDED_TARGET in recorded
        assert PROJECT_REPORTING_SNAPSHOT_TARGET not in recorded

        migrated_executor = MigrationExecutor(connection)
        migrated_executor.migrate([PROJECT_REPORTING_SNAPSHOT_TARGET])
        migrated_apps = migrated_executor.loader.project_state(
            [PROJECT_REPORTING_SNAPSHOT_TARGET]
        ).apps
        MigratedProject = migrated_apps.get_model("evaluations", "EvaluationProject")

        migrated_project = MigratedProject.objects.get(pk=project_pk)
        assert migrated_project.reporting_snapshot == {}
        assert (
            PROJECT_REPORTING_SNAPSHOT_TARGET
            in MigrationRecorder(connection).applied_migrations()
        )

        output = StringIO()
        call_command(
            "recompute_project",
            project=str(project_public_id),
            stdout=output,
        )
        assert "recomputed=1" in output.getvalue()

        current_project = EvaluationProject.objects.get(pk=project_pk)
        with pytest.raises(TemplateStructureError) as summary_error:
            export_summary_workbook(current_project)
        assert summary_error.value.code == "REPORTING_SNAPSHOT_UNAVAILABLE"
        with pytest.raises(TemplateStructureError) as issue_error:
            export_issue_workbook(current_project)
        assert issue_error.value.code == "REPORTING_SNAPSHOT_UNAVAILABLE"
        with pytest.raises(RawExportError) as raw_error:
            export_raw_zip(current_project, actor=hr_admin)
        assert raw_error.value.code == "REPORTING_SNAPSHOT_UNAVAILABLE"
    finally:
        MigrationExecutor(connection).migrate(current_leaves)
