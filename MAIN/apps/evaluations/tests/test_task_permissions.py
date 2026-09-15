import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_evaluator_only_sees_own_tasks(client, evaluator_user, own_task, other_task):
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_list"))

    content = response.content.decode()
    assert response.status_code == 200
    assert own_task.subject.name in content
    assert other_task.subject.name not in content


@pytest.mark.django_db
def test_forwarded_task_url_does_not_disclose_task(client, evaluator_user, other_task):
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_detail", args=[other_task.public_id]))

    assert response.status_code == 404
    assert other_task.subject.name not in response.content.decode()
