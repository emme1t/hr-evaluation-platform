from django.db import migrations, models


def mark_legacy_payloads_unavailable(apps, schema_editor):
    Attempt = apps.get_model("notifications", "NotificationAttempt")
    for attempt in Attempt.objects.only("pk", "channel").iterator(chunk_size=500):
        Attempt.objects.filter(pk=attempt.pk).update(
            payload_snapshot={
                "version": 1,
                "channel": attempt.channel,
                "unavailable": True,
            }
        )


class Migration(migrations.Migration):
    dependencies = [("notifications", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="notificationattempt",
            name="payload_snapshot",
            field=models.JSONField(default=dict),
        ),
        migrations.RunPython(
            mark_legacy_payloads_unavailable,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
