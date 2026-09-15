import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_legacy_attempt_migration_marks_preview_unavailable_without_reconstruction(
    active_project, evaluator
):
    current_leaves = MigrationExecutor(connection).loader.graph.leaf_nodes()
    try:
        executor = MigrationExecutor(connection)
        executor.migrate([("notifications", "0001_initial")])
        old_apps = executor.loader.project_state(
            [("notifications", "0001_initial")]
        ).apps
        Outbox = old_apps.get_model("notifications", "NotificationOutbox")
        Attempt = old_apps.get_model("notifications", "NotificationAttempt")
        outbox = Outbox.objects.create(
            project_id=active_project.pk,
            recipient_id=evaluator.pk,
            task_count=1,
            task_digest="0" * 64,
        )
        attempt = Attempt.objects.create(
            outbox_id=outbox.pk,
            project_id=active_project.pk,
            recipient_id=evaluator.pk,
            task_count=1,
            channel="wecom",
            attempt=1,
        )

        executor = MigrationExecutor(connection)
        executor.migrate(
            [("notifications", "0002_notificationattempt_payload_snapshot")]
        )
        new_apps = executor.loader.project_state(
            [("notifications", "0002_notificationattempt_payload_snapshot")]
        ).apps
        migrated = new_apps.get_model("notifications", "NotificationAttempt").objects.get(
            pk=attempt.pk
        )

        assert migrated.payload_snapshot == {
            "version": 1,
            "channel": "wecom",
            "unavailable": True,
        }
    finally:
        MigrationExecutor(connection).migrate(current_leaves)
