from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from apps.reporting.services.summary import export_summary_workbook


pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="requires real PostgreSQL concurrent connection evidence",
    ),
]


def test_postgresql_concurrent_summary_exports_do_not_mutate_business_state(
    project_results,
):
    project = project_results.project
    before = (
        list(project.subjects.values()),
        list(project.tasks.values()),
        list(project.aggregate_results.values()),
    )

    def export_once():
        connections.close_all()
        return export_summary_workbook(project)

    with ThreadPoolExecutor(max_workers=3) as executor:
        artifacts = list(executor.map(lambda _index: export_once(), range(3)))
    connections.close_all()
    after = (
        list(project.subjects.values()),
        list(project.tasks.values()),
        list(project.aggregate_results.values()),
    )
    assert all(artifacts)
    assert after == before
