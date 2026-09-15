# 人事评价系统云端正式版实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `MAIN` 中构建可通过企业微信认证、网页填写、HR 管理、50/30/20 汇总并安全部署到云服务器的人事评价系统。

**Architecture:** 使用 Django 单体应用划分 `accounts`、`roster`、`evaluations`、`notifications`、`reporting` 和 `audit` 六个业务边界。PostgreSQL 保存数据，Django 服务端模板提供 HR 与员工网页，Nginx 和 Docker Compose 负责预发布与生产部署；企业微信为主要认证和通知入口，企业邮箱为一次性网页链接备用通道。

**Tech Stack:** Python 3.12、Django 5.2 LTS、PostgreSQL 16、Gunicorn 23、Nginx、Docker Compose、httpx、openpyxl、pytest、pytest-django、Playwright。

**Execution Context:** Git 仓库根目录是 `C:\Users\ASUS\Documents\人事评价`，`MAIN/` 是正式代码唯一允许写入的子目录。实施会话和工作树从仓库根目录执行；测试与 Django 命令先 `cd MAIN`，Git 暂存路径使用 `MAIN/...`。

## Global Constraints

- 所有正式代码、测试、迁移、规格和部署文件只能写入 `MAIN/`。
- 不复制历史 HTML Demo 作为生产代码，只复用已批准的字段、文案和页面结构。
- 不提交真实员工、企业邮箱、评价结果、上传文件、数据库、企业微信 Secret、SMTP 授权码或生产 `.env`。
- 使用脱敏虚构数据编写测试；外部标识使用不可猜测 UUID，URL 不包含姓名、邮箱或员工编号。
- 企业微信 OAuth 和工作消息是主通道；企业邮箱只发送有时效、使用后失效的备用网页链接。
- 项目发送后冻结模板、关系、权重和规则；后续修改只影响新项目。
- 默认关系权重为上级 50%、同部门 30%、跨部门 20%；缺少任一要求关系组时总分必须为空并标记“数据不完整”。
- 汇总 Excel 只能填写既有字段，不增加工作表、列、公式，不修改列宽和样式。
- MVP 不引入微服务、Redis、消息队列、前后端分离 SPA 或云之家接入。
- 每个功能严格执行 RED → GREEN → REFACTOR，并在本任务全部测试通过后提交。
- 生产数据库不开放公网端口；生产发布必须先备份、在预发布验证迁移，并保留可回切镜像。

---

## Target File Structure

```text
MAIN/
├─ .gitignore
├─ pyproject.toml
├─ uv.lock
├─ manage.py
├─ .env.example
├─ config/
│  ├─ settings/{base,local,production}.py
│  ├─ urls.py
│  └─ wsgi.py
├─ apps/
│  ├─ core/          # 健康检查、公共类型、系统设置
│  ├─ accounts/      # 自定义用户、企业微信 OAuth、角色
│  ├─ roster/        # 花名册、类别、关系、导入预检
│  ├─ evaluations/   # 模板、项目、任务、草稿、提交、汇总
│  ├─ notifications/ # 企业微信通知、邮件备用链接、发送日志
│  ├─ reporting/     # 原始数据、汇总表、异常说明导出
│  └─ audit/         # 不可由普通界面修改的操作日志
├─ templates/
│  ├─ accounts/
│  ├─ evaluator/
│  └─ hr/
├─ static/
│  ├─ css/app.css
│  └─ js/draft.js
├─ tests/
│  ├─ conftest.py
│  ├─ factories.py
│  ├─ fixtures/
│  ├─ e2e/
│  └─ helpers.py
├─ deploy/
│  ├─ Dockerfile
│  ├─ compose.staging.yml
│  ├─ compose.production.yml
│  ├─ nginx.conf
│  ├─ backup.sh
│  ├─ restore.sh
│  └─ runbooks/{deploy,rollback,backup-restore,pilot}.md
└─ docs/superpowers/{specs,plans}/
```

---

## Test Fixture Contract

`MAIN/tests/factories.py` 只创建虚构数据，并随领域任务逐步增加以下稳定接口：

- `create_category(code: str, name: str) -> EmployeeCategory`
- `create_employee(employee_no: str, name: str, department_level_1: str = "测试中心", department_level_2: str = "测试组", category: EmployeeCategory | None = None, corporate_email: str | None = None, wecom_userid: str | None = None, user: User | None = None) -> Employee`
- `create_user_with_role(role: str) -> User`
- `create_template(category: EmployeeCategory, version: int = 1, item_weights: tuple[str, ...] = ("0.60", "0.40")) -> FormTemplate`
- `create_project(subjects: list[Employee], deadline: datetime, status: str = "draft", relationship_weights: dict[str, str] | None = None, required_groups: tuple[str, ...] = ("manager", "same_department", "cross_department")) -> EvaluationProject`
- `create_task(project: EvaluationProject, evaluator: Employee, subject: Employee, relationship_type: str) -> EvaluationTask`
- `create_final_submission(task: EvaluationTask, item_scores: dict[str, int]) -> Submission`
- `create_hr_user(role: str) -> User`

`MAIN/tests/conftest.py` 从上述工厂组合测试所用 fixtures。每个任务只能在对应模型已实现后增加 fixture；fixture 名称与本计划测试代码保持一致：Task 2 增加 `hr_admin`、`user_with_role`、`employee_set`；Task 4 增加 `roster_workbook_bytes`、`relationship_workbook_bytes`、`relationship_case_bytes`；Task 5 增加 `template_v1`、`template_factory`；Task 6 增加 `draft_project`、`active_project`；Task 7 增加 `evaluator_user`、`own_task`、`other_task`；Task 8 增加 `task`、`complete_answers`；Task 9 增加 `final_submission`、`complete_subject_result_data`、`incomplete_subject_result_data`；Task 10 增加 `evaluator`、`project_with_three_tasks`、`fake_wecom`；Task 11 增加 `template_bytes`、`project_results`、`incomplete_result`；Task 13 增加 `expired_active_project`；Task 15 在 `tests/e2e/conftest.py` 增加 `pilot_data` 和 `pilot_files`。Excel fixtures 由 `MAIN/tests/helpers.py` 在内存中生成，不读取真实花名册或汇总文件。

---

### Task 1: Django 基础工程、配置和健康检查

**Files:**
- Create: `MAIN/pyproject.toml`
- Create: `MAIN/uv.lock`
- Create: `MAIN/manage.py`
- Create: `MAIN/.gitignore`
- Create: `MAIN/.env.example`
- Create: `MAIN/config/settings/base.py`
- Create: `MAIN/config/settings/local.py`
- Create: `MAIN/config/settings/production.py`
- Create: `MAIN/config/urls.py`
- Create: `MAIN/config/wsgi.py`
- Create: `MAIN/apps/accounts/models.py`
- Create: `MAIN/apps/core/views.py`
- Create: `MAIN/tests/conftest.py`
- Create: `MAIN/tests/factories.py`
- Create: `MAIN/tests/helpers.py`
- Test: `MAIN/apps/core/tests/test_health.py`

**Interfaces:**
- Consumes: 无；这是后续所有任务的工程基础。
- Produces: `accounts.User` 自定义用户模型；`GET /health/` 返回应用与数据库状态；`config.settings.local` 和 `config.settings.production` 两套配置入口。

- [ ] **Step 1: 创建依赖清单并锁定版本**

```toml
[project]
name = "enpower-hr-evaluation"
version = "0.1.0"
requires-python = ">=3.12,<3.13"
dependencies = [
  "Django>=5.2,<5.3",
  "psycopg[binary]>=3.2,<3.3",
  "gunicorn>=23,<24",
  "httpx>=0.28,<0.29",
  "openpyxl>=3.1,<3.2",
]

[project.optional-dependencies]
dev = [
  "pytest>=8.3,<9",
  "pytest-django>=4.10,<5",
  "pytest-playwright>=0.7,<1",
  "pytest-cov>=6,<7",
  "freezegun>=1.5,<2",
  "pip-audit>=2.7,<3",
  "playwright>=1.50,<2",
]

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
python_files = ["test_*.py"]
addopts = "-q --strict-markers"
```

```gitignore
.env
.env.*
!.env.example
__pycache__/
.pytest_cache/
.coverage
htmlcov/
.venv/
*.sqlite3
media/
private_uploads/
outputs/
backups/
playwright-report/
test-results/
```

Run: `cd MAIN && uv lock && uv sync --extra dev`

Expected: 生成 `uv.lock`，依赖安装成功，未创建生产凭据文件。

- [ ] **Step 2: 写健康检查的失败测试**

```python
import pytest


@pytest.mark.django_db
def test_health_reports_application_and_database(client):
    response = client.get("/health/")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/core/tests/test_health.py -v`

Expected: FAIL，因为 Django 工程或 `/health/` 路由尚不存在。

- [ ] **Step 4: 创建最小工程和健康检查**

```python
# MAIN/apps/accounts/models.py
import uuid
from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
```

```python
# MAIN/apps/core/views.py
from django.db import connection
from django.http import JsonResponse


def health(request):
    connection.ensure_connection()
    return JsonResponse({"status": "ok", "database": "ok"})
```

在 `base.py` 设置 `AUTH_USER_MODEL = "accounts.User"`，从环境变量读取 `SECRET_KEY` 和数据库连接；`local.py` 允许本地调试，`production.py` 强制 HTTPS、安全 Cookie 和显式 `ALLOWED_HOSTS`。`.env.example` 只列变量名和虚构值。

```python
# MAIN/tests/conftest.py
import pytest


@pytest.fixture(autouse=True)
def force_test_environment(settings):
    settings.APP_ENV = "test"
    settings.SECRET_KEY = "test-only-secret-key-not-for-production"
```

`tests/factories.py` 和 `tests/helpers.py` 先创建为仅含模块说明的空测试支持模块，后续任务按“Test Fixture Contract”逐步增加虚构数据工厂，禁止从 `outputs/` 或历史 Demo 读取数据。

- [ ] **Step 5: 运行基础验证并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations accounts && uv run python manage.py migrate && uv run pytest apps/core/tests/test_health.py -v && uv run python manage.py check`

Expected: 健康检查测试 PASS，Django system check 无错误。

- [ ] **Step 6: 提交基础工程**

```bash
git add MAIN/.gitignore MAIN/pyproject.toml MAIN/uv.lock MAIN/manage.py MAIN/.env.example MAIN/config MAIN/apps/accounts MAIN/apps/core MAIN/tests/conftest.py MAIN/tests/factories.py MAIN/tests/helpers.py
git commit -m "feat: scaffold production Django application"
```

---

### Task 2: 花名册、员工类别和角色数据模型

**Files:**
- Create: `MAIN/apps/roster/models.py`
- Create: `MAIN/apps/roster/services.py`
- Create: `MAIN/apps/roster/migrations/0001_initial.py`
- Modify: `MAIN/config/settings/base.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/roster/tests/test_models.py`
- Test: `MAIN/apps/roster/tests/test_roles.py`

**Interfaces:**
- Consumes: Task 1 的 `accounts.User`。
- Produces: `EmployeeCategory`、`Employee`；`ensure_default_groups()` 创建 `HR_ADMIN`、`HR_OPERATOR`；`Employee.user` 将企业微信认证用户映射到员工。

- [ ] **Step 1: 写模型约束的失败测试**

```python
import pytest
from django.db import IntegrityError
from apps.roster.models import Employee, EmployeeCategory


@pytest.mark.django_db
def test_employee_number_and_wecom_userid_are_unique():
    category = EmployeeCategory.objects.create(code="HR", name="人力资源")
    Employee.objects.create(
        employee_no="E001", name="测试甲", corporate_email="a@example.test",
        category=category, department_level_1="测试中心", department_level_2="测试一组",
        wecom_userid="wx_e001",
    )

    with pytest.raises(IntegrityError):
        Employee.objects.create(
            employee_no="E001", name="测试乙", corporate_email="b@example.test",
            category=category, department_level_1="测试中心", department_level_2="测试二组",
            wecom_userid="wx_e002",
        )
```

- [ ] **Step 2: 写默认角色的失败测试**

```python
import pytest
from django.contrib.auth.models import Group
from apps.roster.services import ensure_default_groups


@pytest.mark.django_db
def test_default_hr_groups_are_idempotent():
    ensure_default_groups()
    ensure_default_groups()

    assert set(Group.objects.values_list("name", flat=True)) >= {"HR_ADMIN", "HR_OPERATOR"}
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/roster/tests/test_models.py apps/roster/tests/test_roles.py -v`

Expected: FAIL，因为 `roster` 模型和服务尚不存在。

- [ ] **Step 4: 实现模型和角色初始化**

```python
# MAIN/apps/roster/models.py
import uuid
from django.conf import settings
from django.db import models


class EmployeeCategory(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100, unique=True)
    is_active = models.BooleanField(default=True)


class Employee(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    employee_no = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    corporate_email = models.EmailField(unique=True)
    department_level_1 = models.CharField(max_length=120)
    department_level_2 = models.CharField(max_length=120)
    category = models.ForeignKey(EmployeeCategory, on_delete=models.PROTECT)
    manager = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    wecom_userid = models.CharField(max_length=128, unique=True, null=True, blank=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    is_active = models.BooleanField(default=True)
```

```python
# MAIN/apps/roster/services.py
from django.contrib.auth.models import Group


def ensure_default_groups() -> None:
    for name in ("HR_ADMIN", "HR_OPERATOR", "EVALUATOR"):
        Group.objects.get_or_create(name=name)
```

通过数据迁移调用 `ensure_default_groups()`，不要依赖管理员手工创建角色。
在 `tests/factories.py` 实现 `create_category()`、`create_employee()`、`create_user_with_role()` 和只接受 `HR_ADMIN`/`HR_OPERATOR` 的 `create_hr_user()`；在 `tests/conftest.py` 提供 `hr_admin`、`user_with_role` 和至少包含三名虚构员工的 `employee_set`，所有默认姓名和邮箱使用 `example.test` 虚构域名。

- [ ] **Step 5: 生成迁移并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations roster && uv run python manage.py migrate && uv run pytest apps/roster/tests/test_models.py apps/roster/tests/test_roles.py -v`

Expected: 两组测试全部 PASS，数据库约束生效。

- [ ] **Step 6: 提交人员基础模型**

```bash
git add MAIN/apps/roster MAIN/config/settings/base.py MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: add roster and HR role models"
```

---

### Task 3: 企业微信 OAuth 登录和员工绑定

**Files:**
- Create: `MAIN/apps/accounts/wecom.py`
- Create: `MAIN/apps/accounts/views.py`
- Create: `MAIN/apps/accounts/urls.py`
- Create: `MAIN/templates/accounts/login_error.html`
- Modify: `MAIN/config/urls.py`
- Modify: `MAIN/config/settings/base.py`
- Test: `MAIN/apps/accounts/tests/test_wecom_oauth.py`
- Test: `MAIN/apps/accounts/tests/test_access_binding.py`

**Interfaces:**
- Consumes: `Employee.wecom_userid`、`Employee.user`。
- Produces: `WeComClient.authorization_url(state, redirect_uri)`、`WeComClient.user_id_from_code(code)`、`GET /auth/wecom/start/`、`GET /auth/wecom/callback/`。

- [ ] **Step 1: 写 OAuth state 与绑定失败测试**

```python
import pytest
from django.urls import reverse
from apps.roster.models import Employee, EmployeeCategory


@pytest.mark.django_db
def test_callback_rejects_invalid_state(client, monkeypatch):
    session = client.session
    session["wecom_oauth_state"] = "expected-state"
    session.save()

    response = client.get(reverse("accounts:wecom_callback"), {"state": "wrong", "code": "abc"})

    assert response.status_code == 403


@pytest.mark.django_db
def test_callback_logs_in_bound_employee(client, monkeypatch, django_user_model):
    category = EmployeeCategory.objects.create(code="OPS", name="运营")
    employee = Employee.objects.create(
        employee_no="E101", name="测试员工", corporate_email="e101@example.test",
        category=category, department_level_1="运营", department_level_2="项目",
        wecom_userid="wx_e101",
    )
    session = client.session
    session["wecom_oauth_state"] = "valid-state"
    session["wecom_oauth_next"] = "/tasks/"
    session.save()
    monkeypatch.setattr("apps.accounts.views.get_wecom_client", lambda: FakeWeComClient("wx_e101"))

    response = client.get(reverse("accounts:wecom_callback"), {"state": "valid-state", "code": "ok"})

    employee.refresh_from_db()
    assert response.status_code == 302
    assert response.url == "/tasks/"
    assert employee.user_id is not None
    assert client.session["_auth_user_id"] == str(employee.user_id)
```

在测试文件定义 `FakeWeComClient.user_id_from_code()`，不得调用真实企业微信。

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/accounts/tests/test_wecom_oauth.py apps/accounts/tests/test_access_binding.py -v`

Expected: FAIL，因为 OAuth 客户端、路由和绑定逻辑尚不存在。

- [ ] **Step 3: 实现企业微信客户端边界**

```python
# MAIN/apps/accounts/wecom.py
from dataclasses import dataclass
from urllib.parse import urlencode
import httpx


@dataclass(frozen=True)
class WeComSettings:
    corp_id: str
    agent_id: str
    secret: str


class WeComClient:
    def __init__(self, settings: WeComSettings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.client = httpx.Client(timeout=10, transport=transport)

    def authorization_url(self, state: str, redirect_uri: str) -> str:
        query = urlencode({"appid": self.settings.corp_id, "redirect_uri": redirect_uri,
                           "response_type": "code", "scope": "snsapi_base", "state": state})
        return f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect"

    def user_id_from_code(self, code: str) -> str:
        token = self._access_token()
        response = self.client.get("https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
                                   params={"access_token": token, "code": code})
        response.raise_for_status()
        payload = response.json()
        if payload.get("errcode") != 0 or not payload.get("userid"):
            raise ValueError("企业微信未返回内部员工身份")
        return payload["userid"]
```

`_access_token()` 只在内存中短期缓存令牌，不记录 Secret、令牌或完整回调参数。

- [ ] **Step 4: 实现登录开始和回调视图**

`start` 使用 `secrets.token_urlsafe(32)` 生成 state，并只接受站内 `next` 路径；`callback` 使用 `hmac.compare_digest` 校验 state，通过 `wecom_userid` 查找启用员工，创建或复用 `accounts.User`，绑定 `Employee.user` 后调用 Django `login()`。未绑定、停用、外部联系人或 API 异常统一返回脱敏错误页并写安全日志。

- [ ] **Step 5: 运行 OAuth 测试并确认 GREEN**

Run: `cd MAIN && uv run pytest apps/accounts/tests -v`

Expected: state、员工绑定、停用员工、开放重定向阻断和错误脱敏测试全部 PASS。

- [ ] **Step 6: 提交企业微信认证**

```bash
git add MAIN/apps/accounts MAIN/templates/accounts MAIN/config
git commit -m "feat: authenticate employees with WeCom OAuth"
```

---

### Task 4: 花名册、类别和协作关系维护与导入预检

**Files:**
- Create: `MAIN/apps/roster/imports.py`
- Create: `MAIN/apps/roster/forms.py`
- Create: `MAIN/apps/roster/views.py`
- Create: `MAIN/apps/roster/urls.py`
- Create: `MAIN/templates/hr/roster/list.html`
- Create: `MAIN/templates/hr/roster/edit.html`
- Create: `MAIN/templates/hr/roster/import_preview.html`
- Create: `MAIN/templates/hr/categories/list.html`
- Create: `MAIN/templates/hr/categories/edit.html`
- Create: `MAIN/templates/hr/relationships/list.html`
- Create: `MAIN/templates/hr/relationships/edit.html`
- Modify: `MAIN/apps/roster/models.py`
- Modify: `MAIN/tests/helpers.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/roster/tests/test_roster_import.py`
- Test: `MAIN/apps/roster/tests/test_relationship_import.py`
- Test: `MAIN/apps/roster/tests/test_manual_maintenance.py`

**Interfaces:**
- Consumes: `Employee`、`EmployeeCategory`、当前登录 HR 用户。
- Produces: `ImportBatch`、`ImportIssue`、`EvaluationRelationship`；`preview_roster_upload()`、`preview_relationship_upload()`、`commit_import_batch(batch_id, mode, duplicate_policy, actor)`；`create_employee_record()`、`update_employee_record()`、`deactivate_employee()`、`create_category_record()`、`update_category_record()`、`upsert_relationship()` 和 `deactivate_relationship()`。

- [ ] **Step 1: 写花名册预检、重复策略和不直接落库的失败测试**

```python
import pytest
from apps.roster.imports import preview_roster_upload, commit_import_batch
from apps.roster.models import Employee


@pytest.mark.django_db
def test_roster_preview_requires_explicit_duplicate_policy(roster_workbook_bytes, employee_set, hr_admin):
    before = Employee.objects.count()
    batch = preview_roster_upload(roster_workbook_bytes, "roster.xlsx", hr_admin)

    assert Employee.objects.count() == before
    assert batch.valid_count == 2
    assert batch.duplicate_count == 1

    result = commit_import_batch(
        batch.public_id, mode="append", duplicate_policy="skip", actor=hr_admin,
    )
    assert result.skipped_duplicate_count == 1
    assert Employee.objects.count() == before + 1
```

`roster_workbook_bytes` 固定包含一名已存在员工和一名新员工；同一批次另测 `duplicate_policy="update"` 只更新现有员工，`mode="replace"` 会在预览中列出将停用的缺失员工并要求二次确认。

- [ ] **Step 2: 写关系导入不直接落库的失败测试**

```python
import pytest
from apps.roster.imports import preview_relationship_upload, commit_import_batch
from apps.roster.models import Employee, EvaluationRelationship


@pytest.mark.django_db
def test_relationship_preview_does_not_write_relationships(employee_set, relationship_workbook_bytes, hr_admin):
    batch = preview_relationship_upload(relationship_workbook_bytes, "relations.xlsx", hr_admin)

    assert batch.valid_count == 3
    assert EvaluationRelationship.objects.count() == 0

    commit_import_batch(batch.public_id, mode="replace", duplicate_policy="update", actor=hr_admin)
    assert EvaluationRelationship.objects.count() == 3
```

- [ ] **Step 3: 写关系边界和手工维护失败测试**

```python
import pytest
from apps.roster.imports import preview_relationship_upload
from apps.roster.models import EmployeeCategory
from apps.roster.services import (
    RosterValidationError,
    create_employee_record,
    upsert_relationship,
)


@pytest.mark.django_db
@pytest.mark.parametrize("case,expected_code", [
    ("self", "SELF_RELATION"),
    ("missing", "EMPLOYEE_NOT_FOUND"),
    ("same_department_mismatch", "SAME_DEPARTMENT_REQUIRED"),
    ("cross_department_mismatch", "CROSS_DEPARTMENT_REQUIRED"),
    ("duplicate", "DUPLICATE_RELATION"),
])
def test_relationship_preview_reports_invalid_rows(case, expected_code, relationship_case_bytes, hr_admin):
    batch = preview_relationship_upload(relationship_case_bytes(case), "relations.xlsx", hr_admin)

    assert expected_code in set(batch.issues.values_list("code", flat=True))


@pytest.mark.django_db
def test_manual_employee_rejects_inactive_category(hr_admin):
    category = EmployeeCategory.objects.create(code="OLD", name="已停用类别", is_active=False)

    with pytest.raises(RosterValidationError, match="员工类别已停用"):
        create_employee_record(
            employee_no="E900", name="测试人员", corporate_email="e900@example.test",
            department_level_1="测试中心", department_level_2="测试组",
            category_id=category.id, actor=hr_admin,
        )


@pytest.mark.django_db
def test_manual_relationship_matches_by_employee_number(employee_set, hr_admin):
    relation = upsert_relationship(
        subject_no=employee_set[0].employee_no,
        evaluator_no=employee_set[1].employee_no,
        relationship_type="same_department",
        actor=hr_admin,
    )

    assert relation.subject_id == employee_set[0].id
    assert relation.evaluator_id == employee_set[1].id
```

- [ ] **Step 4: 运行维护与导入测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/roster/tests/test_roster_import.py apps/roster/tests/test_relationship_import.py apps/roster/tests/test_manual_maintenance.py -v`

Expected: FAIL，因为导入批次、关系模型和预检服务尚不存在。

- [ ] **Step 5: 实现导入模型、维护服务和校验边界**

```python
class EvaluationRelationship(models.Model):
    class Type(models.TextChoices):
        MANAGER = "manager", "上级"
        SAME_DEPARTMENT = "same_department", "同部门协作"
        CROSS_DEPARTMENT = "cross_department", "跨部门协作"

    subject = models.ForeignKey(Employee, related_name="evaluation_subject_relations", on_delete=models.PROTECT)
    evaluator = models.ForeignKey(Employee, related_name="evaluation_assignments", on_delete=models.PROTECT)
    relationship_type = models.CharField(max_length=32, choices=Type.choices)
    is_active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["subject", "evaluator", "relationship_type"],
                condition=models.Q(is_active=True),
                name="unique_active_evaluation_relationship",
            )
        ]
```

`ImportBatch` 保存导入类型、文件 SHA-256、解析后的受控行 JSON、有效数、重复数、异常数、将新增/更新/停用的数量、状态和创建人；`ImportIssue` 保存行号、错误码和不含额外隐私的说明。`commit_import_batch()` 使用事务和 `select_for_update()`，只允许提交一次；`mode` 只接受 `replace`/`append`，存在重复时 `duplicate_policy` 必须显式为 `update`/`skip`。`replace` 不物理删除员工或历史关系，只停用预览中明确列出的记录。

手工维护服务和导入提交复用同一组字段校验：员工编号、企业邮箱和企业微信 UserId 唯一；员工只能选择启用类别；类别编码和名称唯一；员工与关系只允许停用。`upsert_relationship()` 只接受员工编号作为输入，并复用本人、重复和部门一致性校验。
在 `tests/helpers.py` 实现内存 Excel 构造器；在 `tests/conftest.py` 组合 `employee_set`、`relationship_workbook_bytes` 和 `relationship_case_bytes`，不得创建磁盘上的真实数据副本。

- [ ] **Step 6: 实现 HR 手工维护、预览与确认页面**

花名册、类别和协作关系页面提供分页列表、新增、编辑和停用；关系编辑框通过员工编号搜索并显示姓名确认，保存值始终是员工主键。上传接口限制 `.xlsx/.csv` 和 10MB；按员工编号匹配，姓名只用于显示；预览页明确列出新增、更新、跳过、停用和异常数量。确认接口要求 POST、CSRF、HR 角色、覆盖/追加模式和重复更新/跳过策略。视图不得在 GET 请求中写业务数据。

- [ ] **Step 7: 运行维护与导入测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations roster && uv run python manage.py migrate && uv run pytest apps/roster/tests -v`

Expected: 预检、异常、覆盖、追加、重复提交和权限测试全部 PASS。

- [ ] **Step 8: 提交维护、导入与关系功能**

```bash
git add MAIN/apps/roster MAIN/templates/hr/roster MAIN/templates/hr/categories MAIN/templates/hr/relationships MAIN/tests/helpers.py MAIN/tests/conftest.py
git commit -m "feat: validate roster and relationship imports"
```

---

### Task 5: 评价表模板版本和编辑校验

**Files:**
- Create: `MAIN/apps/evaluations/models/templates.py`
- Create: `MAIN/apps/evaluations/services/templates.py`
- Create: `MAIN/apps/evaluations/forms/templates.py`
- Create: `MAIN/apps/evaluations/views/templates.py`
- Create: `MAIN/templates/hr/templates/list.html`
- Create: `MAIN/templates/hr/templates/edit.html`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/evaluations/tests/test_template_versions.py`
- Test: `MAIN/apps/evaluations/tests/test_template_validation.py`

**Interfaces:**
- Consumes: `EmployeeCategory` 和 HR 角色。
- Produces: `FormTemplate`、`TemplateItem`；`create_template_version(source, changes, actor)`；`validate_template(template)`。

- [ ] **Step 1: 写版本不可变和权重校验的失败测试**

```python
import pytest
from decimal import Decimal
from apps.evaluations.services.templates import create_template_version, validate_template


@pytest.mark.django_db
def test_editing_template_creates_new_version_and_preserves_source(template_v1, hr_admin):
    template_v2 = create_template_version(
        template_v1,
        changes={"name": "人力资源专员评价表 2026", "items": template_v1.item_payloads()},
        actor=hr_admin,
    )

    template_v1.refresh_from_db()
    assert template_v1.version == 1
    assert template_v2.version == 2
    assert template_v2.previous_version_id == template_v1.id


@pytest.mark.django_db
def test_template_item_weights_must_sum_to_one(template_factory):
    template = template_factory(item_weights=("0.60", "0.30"))

    errors = validate_template(template)

    assert errors == ["评价项权重合计必须为 100%"]
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_template_versions.py apps/evaluations/tests/test_template_validation.py -v`

Expected: FAIL，因为模板模型和版本服务尚不存在。

- [ ] **Step 3: 实现模板和评价项模型**

```python
class FormTemplate(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    category = models.ForeignKey("roster.EmployeeCategory", on_delete=models.PROTECT)
    name = models.CharField(max_length=160)
    version = models.PositiveIntegerField()
    previous_version = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["category", "version"], name="unique_category_template_version")]


class TemplateItem(models.Model):
    template = models.ForeignKey(FormTemplate, related_name="items", on_delete=models.CASCADE)
    group = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    order = models.PositiveIntegerField()
    weight = models.DecimalField(max_digits=6, decimal_places=5)
    score_min = models.PositiveSmallIntegerField(default=1)
    score_max = models.PositiveSmallIntegerField(default=5)
    excellent_description = models.TextField()
    good_description = models.TextField()
    qualified_description = models.TextField()
    improvement_description = models.TextField()
```

`create_template_version()` 在事务中复制旧项并应用完整变更，不允许原地更新已保存版本。`validate_template()` 检查至少一个项目、顺序唯一、标题非空、评分范围有效以及权重精确合计 `Decimal("1.00000")`。
为 `FormTemplate` 实现 `item_payloads()`，只返回创建下一版本所需的项目字段；在测试工厂增加 `create_template()`，并在 `conftest.py` 提供 `template_v1` 和 `template_factory`。

- [ ] **Step 4: 实现 HR 模板编辑页面**

使用 Django formset 编辑项目和四档描述；保存时创建新版本。删除项目是新版本中的显式删除，不修改旧版本。正在被项目快照使用的版本仍可读取但不能删除。

- [ ] **Step 5: 运行模板测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations evaluations && uv run python manage.py migrate && uv run pytest apps/evaluations/tests/test_template_versions.py apps/evaluations/tests/test_template_validation.py -v`

Expected: 版本复制、旧版本不可变、权重、描述和权限测试全部 PASS。

- [ ] **Step 6: 提交模板版本功能**

```bash
git add MAIN/apps/evaluations MAIN/templates/hr/templates MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: version evaluation form templates"
```

---

### Task 6: 项目快照和评价任务生成

**Files:**
- Create: `MAIN/apps/evaluations/models/projects.py`
- Create: `MAIN/apps/evaluations/services/projects.py`
- Create: `MAIN/apps/evaluations/forms/projects.py`
- Create: `MAIN/apps/evaluations/views/projects.py`
- Create: `MAIN/templates/hr/projects/create.html`
- Create: `MAIN/templates/hr/projects/preview.html`
- Create: `MAIN/templates/hr/projects/detail.html`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/evaluations/tests/test_project_snapshot.py`
- Test: `MAIN/apps/evaluations/tests/test_task_generation.py`
- Test: `MAIN/apps/evaluations/tests/test_project_lifecycle.py`

**Interfaces:**
- Consumes: 启用员工、有效关系、已验证模板版本。
- Produces: `EvaluationProject`、`ProjectSubject`、`EvaluationTask`；`preview_project_tasks(project)`；`prepare_project(project, actor)` 将草稿冻结为待发送项目，但不调用外部通知通道；`extend_project_deadline(project, deadline, actor)`；`close_project_early(project, actor)`。

- [ ] **Step 1: 写项目快照失败测试**

```python
import pytest
from apps.evaluations.services.projects import prepare_project


@pytest.mark.django_db
def test_prepare_freezes_template_relationships_and_weights(draft_project, hr_admin):
    prepare_project(draft_project, hr_admin)

    subject = draft_project.subjects.get()
    assert subject.template_snapshot["version"] == 1
    assert subject.relationship_snapshot
    assert draft_project.rule_snapshot == {
        "manager": "0.50", "same_department": "0.30", "cross_department": "0.20",
        "required_groups": ["manager", "same_department", "cross_department"],
    }
```

- [ ] **Step 2: 写任务唯一性和项目生命周期失败测试**

```python
@pytest.mark.django_db
def test_prepare_is_idempotent_and_does_not_duplicate_tasks(draft_project, hr_admin):
    first = prepare_project(draft_project, hr_admin)
    second = prepare_project(draft_project, hr_admin)

    assert first.created_count == second.total_count
    assert second.created_count == 0
    assert draft_project.tasks.count() == first.created_count
```

```python
from datetime import timedelta
import pytest
from django.utils import timezone
from apps.evaluations.services.projects import (
    ProjectStateError,
    close_project_early,
    extend_project_deadline,
)


@pytest.mark.django_db
def test_active_project_deadline_can_only_move_later(active_project, hr_admin):
    later = active_project.deadline + timedelta(days=2)
    extend_project_deadline(active_project, later, hr_admin)
    active_project.refresh_from_db()
    assert active_project.deadline == later

    with pytest.raises(ProjectStateError, match="截止时间只能向后延长"):
        extend_project_deadline(active_project, timezone.now(), hr_admin)


@pytest.mark.django_db
def test_close_project_early_is_idempotent(active_project, hr_admin):
    close_project_early(active_project, hr_admin)
    close_project_early(active_project, hr_admin)
    active_project.refresh_from_db()
    assert active_project.status == "closed"
```

- [ ] **Step 3: 运行测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_project_snapshot.py apps/evaluations/tests/test_task_generation.py apps/evaluations/tests/test_project_lifecycle.py -v`

Expected: FAIL，因为项目、快照和任务服务尚不存在。

- [ ] **Step 4: 实现项目和任务模型**

```python
class EvaluationProject(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        READY = "ready", "待发送"
        ACTIVE = "active", "进行中"
        CLOSED = "closed", "已截止"
        ARCHIVED = "archived", "已归档"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    deadline = models.DateTimeField()
    rule_snapshot = models.JSONField(default=dict)
    prepared_at = models.DateTimeField(null=True, blank=True)
    launched_at = models.DateTimeField(null=True, blank=True)


class EvaluationTask(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    project = models.ForeignKey(EvaluationProject, related_name="tasks", on_delete=models.PROTECT)
    evaluator = models.ForeignKey("roster.Employee", related_name="tasks_to_complete", on_delete=models.PROTECT)
    subject = models.ForeignKey("roster.Employee", related_name="evaluation_tasks", on_delete=models.PROTECT)
    relationship_type = models.CharField(max_length=32)
    status = models.CharField(max_length=16, default="pending")

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["project", "evaluator", "subject", "relationship_type"],
            name="unique_project_evaluation_task",
        )]
```

`ProjectSubject` 保存每个被评价人的模板 JSON 快照和关系 JSON 快照。快照中的项目含标题、顺序、权重、评分范围和四档描述，确保历史项目不读取后来版本。
在测试工厂增加 `create_project()` 和 `create_task()`，并在 `conftest.py` 提供带完整三类关系的 `draft_project`，以及用于生命周期边界测试的 `active_project`。

- [ ] **Step 5: 实现预览和发送事务**

`ProjectForm` 默认填入上级 `0.50`、同部门 `0.30`、跨部门 `0.20`，允许 HR 在草稿项目中修改三类关系权重；保存时使用 `Decimal` 校验非负且合计精确为 `1.00`，发送后只读取 `rule_snapshot`。`preview_project_tasks()` 只返回数量、未绑定员工和关系异常，不写任务。`prepare_project()` 使用 `transaction.atomic()` 和项目行锁，把模板、关系和规则快照及任务一次性写入并将状态从 `draft` 改为 `ready`；再次调用只返回既有任务统计。发现模板无效、关系缺失、员工停用或规则权重不等于 1 时拒绝准备。正式进入 `active` 和外部通知由 Task 10 的 `launch_project()` 完成，避免在数据库事务中调用外部接口。

`extend_project_deadline()` 只允许 `ready`/`active` 项目把截止时间向后延长；`close_project_early()` 只允许 `active` 项目转为 `closed` 并把未提交任务标记为 `not_submitted`。两者均加项目行锁、拒绝倒退状态，并由 HR 项目详情页上的 POST+CSRF 操作触发；相应测试覆盖草稿、已归档、缩短截止时间和重复提前截止。

- [ ] **Step 6: 运行项目测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations evaluations && uv run python manage.py migrate && uv run pytest apps/evaluations/tests/test_project_snapshot.py apps/evaluations/tests/test_task_generation.py apps/evaluations/tests/test_project_lifecycle.py -v`

Expected: 快照冻结、任务唯一、重复发送幂等、非法项目拒绝和权限测试全部 PASS。

- [ ] **Step 7: 提交项目和任务生成**

```bash
git add MAIN/apps/evaluations MAIN/templates/hr/projects MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: freeze projects and generate evaluation tasks"
```

---

### Task 7: 员工任务中心和任务归属权限

**Files:**
- Create: `MAIN/apps/evaluations/querysets.py`
- Create: `MAIN/apps/evaluations/views/evaluator.py`
- Create: `MAIN/apps/evaluations/urls.py`
- Create: `MAIN/templates/evaluator/task_list.html`
- Create: `MAIN/templates/evaluator/task_form.html`
- Create: `MAIN/templates/evaluator/forbidden.html`
- Create: `MAIN/static/css/app.css`
- Modify: `MAIN/config/urls.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/evaluations/tests/test_task_permissions.py`
- Test: `MAIN/apps/evaluations/tests/test_task_center.py`

**Interfaces:**
- Consumes: 已登录 `accounts.User`、`Employee.user` 和 `EvaluationTask`。
- Produces: `tasks_for_user(user)`；`GET /tasks/`；`GET /tasks/<uuid:public_id>/`。

- [ ] **Step 1: 写任务隔离失败测试**

```python
import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_evaluator_only_sees_own_tasks(client, evaluator_user, own_task, other_task):
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_list"))

    assert response.status_code == 200
    assert own_task.subject.name in response.content.decode()
    assert other_task.subject.name not in response.content.decode()


@pytest.mark.django_db
def test_forwarded_task_url_does_not_disclose_task(client, evaluator_user, other_task):
    client.force_login(evaluator_user)

    response = client.get(reverse("evaluations:task_detail", args=[other_task.public_id]))

    assert response.status_code == 404
    assert other_task.subject.name not in response.content.decode()
```

- [ ] **Step 2: 运行权限测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_task_permissions.py apps/evaluations/tests/test_task_center.py -v`

Expected: FAIL，因为任务查询和员工页面尚不存在。

- [ ] **Step 3: 实现后端任务归属查询**

```python
def tasks_for_user(user):
    return (
        EvaluationTask.objects
        .filter(evaluator__user=user, evaluator__is_active=True)
        .select_related("project", "subject")
        .order_by("project__deadline", "subject__name")
    )
```

任务详情必须从 `tasks_for_user(request.user)` 再按 `public_id` 查询，禁止先取全局任务再在模板中判断。
在 `conftest.py` 用测试工厂提供 `evaluator_user`、`own_task` 和属于另一评价人的 `other_task`。

- [ ] **Step 4: 实现移动端任务中心页面**

任务中心按项目显示待填写、草稿、已提交和截止状态；具体表单从 `ProjectSubject.template_snapshot` 渲染，不读取当前模板。CSS 使用单列移动布局，评分控件可触摸，页面不显示其他评价人或汇总结果。

- [ ] **Step 5: 运行页面与权限测试并确认 GREEN**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_task_permissions.py apps/evaluations/tests/test_task_center.py -v`

Expected: 自有任务可见、他人任务 404、停用员工拒绝、截止任务只读和模板快照渲染测试全部 PASS。

- [ ] **Step 6: 提交员工任务中心**

```bash
git add MAIN/apps/evaluations MAIN/templates/evaluator MAIN/static/css MAIN/config/urls.py MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: add secure evaluator task center"
```

---

### Task 8: 草稿保存、完整性校验和幂等提交

**Files:**
- Create: `MAIN/apps/evaluations/models/submissions.py`
- Create: `MAIN/apps/evaluations/services/submissions.py`
- Create: `MAIN/apps/evaluations/forms/submissions.py`
- Create: `MAIN/static/js/draft.js`
- Modify: `MAIN/apps/evaluations/views/evaluator.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/evaluations/tests/test_drafts.py`
- Test: `MAIN/apps/evaluations/tests/test_submission_validation.py`
- Test: `MAIN/apps/evaluations/tests/test_submission_idempotency.py`

**Interfaces:**
- Consumes: 当前用户拥有的 `EvaluationTask` 和模板快照。
- Produces: `save_draft(task, user, answers)`；`submit_task(task, user, answers, idempotency_key)`；`Submission`、`SubmissionAnswer`。

- [ ] **Step 1: 写草稿和提交失败测试**

```python
import pytest
from apps.evaluations.services.submissions import save_draft, submit_task, SubmissionValidationError


@pytest.mark.django_db
def test_draft_can_be_replaced_before_submission(task, evaluator_user):
    save_draft(task, evaluator_user, {"item-1": 3})
    draft = save_draft(task, evaluator_user, {"item-1": 5})

    assert draft.answers.get(item_snapshot_id="item-1").score == 5


@pytest.mark.django_db
def test_submit_rejects_missing_required_answers(task, evaluator_user):
    with pytest.raises(SubmissionValidationError, match="请完成全部必填评价项"):
        submit_task(task, evaluator_user, {"item-1": 5}, "key-1")
```

- [ ] **Step 2: 写幂等失败测试**

```python
@pytest.mark.django_db(transaction=True)
def test_same_idempotency_key_returns_single_submission(task, evaluator_user, complete_answers):
    first = submit_task(task, evaluator_user, complete_answers, "stable-key")
    second = submit_task(task, evaluator_user, complete_answers, "stable-key")

    assert first.id == second.id
    assert task.submissions.filter(is_final=True).count() == 1
```

- [ ] **Step 3: 运行提交测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_drafts.py apps/evaluations/tests/test_submission_validation.py apps/evaluations/tests/test_submission_idempotency.py -v`

Expected: FAIL，因为提交模型和服务尚不存在。

- [ ] **Step 4: 实现提交模型和事务服务**

```python
class Submission(models.Model):
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    task = models.ForeignKey(EvaluationTask, related_name="submissions", on_delete=models.PROTECT)
    is_final = models.BooleanField(default=False)
    idempotency_key = models.CharField(max_length=128, null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    client_type = models.CharField(max_length=32, default="web")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task"], condition=models.Q(is_final=True), name="one_final_submission_per_task"),
            models.UniqueConstraint(fields=["task", "idempotency_key"], condition=models.Q(idempotency_key__isnull=False), name="unique_task_idempotency_key"),
        ]
```

`submit_task()` 在事务中锁定任务，先检查任务归属、项目状态、截止时间、所有快照项目、分数范围和幂等键，再将草稿原子升级为正式提交并把任务设为 `submitted`。已正式提交且幂等键不同则返回“该任务已提交”。
在 `conftest.py` 提供含两个必填快照项目的 `task` 和覆盖全部项目的 `complete_answers`。

- [ ] **Step 5: 实现草稿接口和浏览器自动保存**

使用 `POST /tasks/<uuid>/draft/`，返回 `saved_at` 和版本号。`draft.js` 在输入后 800ms 防抖保存，并在离线或失败时保留页面值、显示未保存状态；不得把评分写入 `localStorage`。

- [ ] **Step 6: 运行提交测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations evaluations && uv run python manage.py migrate && uv run pytest apps/evaluations/tests/test_drafts.py apps/evaluations/tests/test_submission_validation.py apps/evaluations/tests/test_submission_idempotency.py -v`

Expected: 草稿覆盖、完整性、1～5 边界、权限、截止、幂等和单次正式提交测试全部 PASS。

- [ ] **Step 7: 提交草稿和正式提交功能**

```bash
git add MAIN/apps/evaluations MAIN/static/js/draft.js MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: save drafts and submit evaluations once"
```

---

### Task 9: 评分、关系组均分和 50/30/20 汇总

**Files:**
- Create: `MAIN/apps/evaluations/models/results.py`
- Create: `MAIN/apps/evaluations/services/scoring.py`
- Create: `MAIN/apps/evaluations/management/commands/recompute_project.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/evaluations/tests/test_submission_score.py`
- Test: `MAIN/apps/evaluations/tests/test_aggregate_score.py`
- Test: `MAIN/apps/evaluations/tests/test_incomplete_result.py`

**Interfaces:**
- Consumes: 已正式提交的 `Submission`、评价项快照权重和项目规则快照。
- Produces: `calculate_submission_score(submission) -> Decimal`；`calculate_subject_result(project, subject) -> AggregateOutcome`；`AggregateResult`。

- [ ] **Step 1: 写单份表单加权失败测试**

```python
from decimal import Decimal
from apps.evaluations.services.scoring import calculate_submission_score


def test_submission_score_uses_frozen_item_weights(final_submission):
    assert calculate_submission_score(final_submission) == Decimal("4.20")
```

- [ ] **Step 2: 写关系组和缺失组失败测试**

```python
from decimal import Decimal
from apps.evaluations.services.scoring import calculate_subject_result


def test_complete_result_averages_groups_before_503020_weighting(complete_subject_result_data):
    outcome = calculate_subject_result(**complete_subject_result_data)

    assert outcome.group_scores == {
        "manager": Decimal("4.00"),
        "same_department": Decimal("3.50"),
        "cross_department": Decimal("3.00"),
    }
    assert outcome.total_score == Decimal("3.65")
    assert outcome.status == "complete"


def test_missing_required_group_leaves_total_blank(incomplete_subject_result_data):
    outcome = calculate_subject_result(**incomplete_subject_result_data)

    assert outcome.total_score is None
    assert outcome.status == "incomplete"
    assert outcome.missing_groups == ["manager"]
```

- [ ] **Step 3: 运行评分测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/evaluations/tests/test_submission_score.py apps/evaluations/tests/test_aggregate_score.py apps/evaluations/tests/test_incomplete_result.py -v`

Expected: FAIL，因为评分服务和结果模型尚不存在。

- [ ] **Step 4: 实现纯函数评分和持久化结果**

```python
@dataclass(frozen=True)
class AggregateOutcome:
    group_scores: dict[str, Decimal | None]
    total_score: Decimal | None
    status: str
    missing_groups: list[str]
    valid_submission_count: int


def weighted_total(group_scores, weights, required_groups):
    missing = [group for group in required_groups if group_scores.get(group) is None]
    if missing:
        return None, missing
    total = sum(group_scores[group] * Decimal(weights[group]) for group in required_groups)
    return total.quantize(Decimal("0.01")), []
```

先用快照项目权重计算每份正式表单分数，再对同关系类型表单求算术平均，最后按项目快照权重计算总分。所有中间计算使用 `Decimal`，只在展示和持久化时量化到两位小数。
在测试工厂实现 `create_final_submission()`；`final_submission` fixture 的任务快照固定含权重 `0.60`/`0.40` 的 `item-1`、`item-2`，并以 `{"item-1": 5, "item-2": 3}` 创建答案。`complete_subject_result_data` 与 `incomplete_subject_result_data` 使用同一 `calculate_subject_result(project, subject)` 输入结构。

- [ ] **Step 5: 实现可审计重算命令**

`recompute_project --project <uuid>` 使用事务写入 `AggregateResult`，记录算法版本、输入提交数、计算时间和完整性；重复运行覆盖同一项目当前计算版本，不修改原始提交。

- [ ] **Step 6: 运行评分测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations evaluations && uv run python manage.py migrate && uv run pytest apps/evaluations/tests/test_submission_score.py apps/evaluations/tests/test_aggregate_score.py apps/evaluations/tests/test_incomplete_result.py -v`

Expected: 权重、关系组平均、精度、空数据和缺失组测试全部 PASS。

- [ ] **Step 7: 提交汇总算法**

```bash
git add MAIN/apps/evaluations MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: calculate auditable 50-30-20 results"
```

---

### Task 10: 合并企业微信通知和企业邮箱备用链接

**Files:**
- Create: `MAIN/apps/notifications/models.py`
- Create: `MAIN/apps/notifications/wecom.py`
- Create: `MAIN/apps/notifications/email.py`
- Create: `MAIN/apps/notifications/services.py`
- Create: `MAIN/apps/notifications/views.py`
- Create: `MAIN/apps/notifications/urls.py`
- Create: `MAIN/templates/hr/notifications/project_status.html`
- Create: `MAIN/templates/notifications/email_confirm.html`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/notifications/tests/test_merged_wecom_notice.py`
- Test: `MAIN/apps/notifications/tests/test_notification_failure.py`
- Test: `MAIN/apps/notifications/tests/test_email_magic_link.py`
- Test: `MAIN/apps/notifications/tests/test_email_content.py`

**Interfaces:**
- Consumes: 活跃项目、评价人名下任务、`Employee.wecom_userid` 和企业邮箱。
- Produces: `launch_project(project, actor, notifier)`；`notify_project_evaluator(project, evaluator, channel, notifier)`；`resend_project_notification(project, evaluator, channel, actor)`；`WeComNotifier.send_task_summary()`；`create_email_magic_link()`、`validate_email_magic_link()`、`consume_email_magic_link()`；`NotificationLog`、`EmailMagicLink`。

- [ ] **Step 1: 写合并通知失败测试**

```python
import pytest
from apps.notifications.services import launch_project, notify_project_evaluator


@pytest.mark.django_db
def test_one_wecom_message_summarizes_all_project_tasks(project_with_three_tasks, fake_wecom, evaluator):
    result = notify_project_evaluator(project_with_three_tasks, evaluator, "wecom", notifier=fake_wecom)

    assert result.status == "sent"
    assert fake_wecom.calls == [{
        "userid": evaluator.wecom_userid,
        "title": project_with_three_tasks.name,
        "description": "您有 3 项待评价任务",
        "url": f"https://evaluation.example.test/projects/{project_with_three_tasks.public_id}/tasks/",
    }]


@pytest.mark.django_db
def test_launch_transitions_ready_project_once(project_with_three_tasks, fake_wecom, evaluator, hr_admin):
    first = launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)
    second = launch_project(project_with_three_tasks, hr_admin, notifier=fake_wecom)

    project_with_three_tasks.refresh_from_db()
    assert project_with_three_tasks.status == "active"
    assert first.sent_recipient_count == 1
    assert second.already_launched is True
    assert len(fake_wecom.calls) == 1
```

- [ ] **Step 2: 写备用链接安全失败测试**

```python
import pytest
from django.utils import timezone
from apps.notifications.services import (
    InvalidMagicLink,
    consume_email_magic_link,
    create_email_magic_link,
    notify_project_evaluator,
    validate_email_magic_link,
)


@pytest.mark.django_db
def test_email_magic_link_is_hashed_expires_and_can_only_be_used_once(evaluator, active_project):
    raw_token, link = create_email_magic_link(active_project, evaluator, ttl_hours=24)

    assert raw_token not in link.token_hash
    assert link.expires_at > timezone.now()
    assert validate_email_magic_link(raw_token).id == link.id
    link.refresh_from_db()
    assert link.used_at is None
    assert consume_email_magic_link(raw_token).id == link.id
    with pytest.raises(InvalidMagicLink):
        consume_email_magic_link(raw_token)


@pytest.mark.django_db
def test_email_fallback_uses_utf8_dynamic_names_and_no_attachment(
    active_project, evaluator, mailoutbox,
):
    notify_project_evaluator(active_project, evaluator, "email")

    message = mailoutbox[0]
    assert message.subject == "恩力公司人事部门人事评价项目"
    assert message.body.startswith(f"您好，{evaluator.name}：")
    for name in active_project.tasks.filter(evaluator=evaluator).values_list("subject__name", flat=True):
        assert name in message.body
    assert "??" not in message.body
    assert message.attachments == []
```

- [ ] **Step 3: 运行通知测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/notifications/tests -v`

Expected: FAIL，因为通知模型和服务尚不存在。

- [ ] **Step 4: 实现通知日志和企业微信发送器**

```python
class NotificationLog(models.Model):
    project = models.ForeignKey("evaluations.EvaluationProject", on_delete=models.PROTECT)
    recipient = models.ForeignKey("roster.Employee", on_delete=models.PROTECT)
    tasks = models.ManyToManyField("evaluations.EvaluationTask", related_name="notification_logs")
    channel = models.CharField(max_length=16, choices=[("wecom", "企业微信"), ("email", "企业邮箱")])
    status = models.CharField(max_length=16)
    failure_code = models.CharField(max_length=80, blank=True)
    attempt = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
```

`launch_project()` 对 `ready` 项目加行锁并原子写入 `active`/`launched_at`，事务提交后才逐评价人调用通知接口；重复调用不重复发送。`WeComNotifier` 使用 Task 3 的令牌客户端发送 textcard 或同等工作消息，消息只包含项目名、任务数量、截止时间和个人任务中心 URL。HTTP 超时、企业微信错误码和未绑定分别映射为稳定错误码，日志不保存 access token 或完整响应。单个收件人失败不回滚已进入进行中的项目，而是写失败日志并允许 HR 显式重发或切换邮件通道。
`NotificationLog` 同时保存 `task_count` 并通过 `tasks` 关联本次合并通知覆盖的任务；在 `conftest.py` 提供状态为 `ready` 的 `project_with_three_tasks`、`fake_wecom`、`evaluator` 和状态为 `active` 的 `active_project`。

- [ ] **Step 5: 实现一次性邮件登录**

使用 `secrets.token_urlsafe(32)` 生成原始 token，数据库只保存 `sha256(token)`、员工、项目、过期时间和使用时间。`GET /auth/email/<token>/` 只校验并显示确认页，避免邮件安全扫描器提前消耗链接；用户 POST 确认后才在同一事务中锁定链接、标记使用并登录对应员工。若该员工尚未绑定 `accounts.User`，创建一个不可使用密码登录的本地用户并绑定 `Employee.user`，因此企业微信未绑定人员也能使用经 HR 下发的备用路径；随后重定向任务中心。失败只显示“链接无效或已过期”。邮件使用 Django EmailBackend，主题固定为“恩力公司人事部门人事评价项目”，正文以“您好，{评价人姓名}：”开头，动态列出该评价人在本项目需填写的一个或多个被评价人，不出现硬编码姓名；主题、正文和姓名均以 UTF-8 发送并测试往返后不出现乱码或连续问号。邮件只包含同一 HTTPS 网页入口，不附 Excel 或 `.bin` 文件。

HR 通知状态页按评价人显示覆盖任务数、最近通道、结果、稳定错误码和重试次数；重发与切换邮件均为带 CSRF 的 POST，并调用 `resend_project_notification()` 产生新的日志，不覆盖历史日志。

- [ ] **Step 6: 运行通知测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations notifications && uv run python manage.py migrate && uv run pytest apps/notifications/tests -v`

Expected: 合并发送、失败日志、UTF-8、token 哈希、过期、单次使用和错误脱敏测试全部 PASS。

- [ ] **Step 7: 提交通知通道**

```bash
git add MAIN/apps/notifications MAIN/templates/hr/notifications MAIN/templates/notifications MAIN/config/urls.py MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: notify evaluators through WeCom and email fallback"
```

---

### Task 11: 模板一致的汇总表、原始数据和异常说明导出

**Files:**
- Create: `MAIN/apps/reporting/services/summary.py`
- Create: `MAIN/apps/reporting/services/raw.py`
- Create: `MAIN/apps/reporting/services/issues.py`
- Create: `MAIN/apps/reporting/models.py`
- Create: `MAIN/apps/reporting/forms.py`
- Create: `MAIN/apps/reporting/storage.py`
- Create: `MAIN/apps/reporting/views.py`
- Create: `MAIN/apps/reporting/urls.py`
- Create: `MAIN/templates/hr/reporting/template_list.html`
- Create: `MAIN/templates/hr/reporting/template_upload.html`
- Create: `MAIN/apps/reporting/tests/workbook_helpers.py`
- Modify: `MAIN/apps/evaluations/models/projects.py`
- Modify: `MAIN/apps/evaluations/services/projects.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/reporting/tests/test_summary_export.py`
- Test: `MAIN/apps/reporting/tests/test_summary_template.py`
- Test: `MAIN/apps/reporting/tests/test_raw_export.py`
- Test: `MAIN/apps/reporting/tests/test_issue_export.py`

**Interfaces:**
- Consumes: `AggregateResult`、正式提交、未回收任务、导入异常和项目绑定的汇总模板版本。
- Produces: `SummaryWorkbookTemplate`；`register_summary_template(file_bytes, filename, actor)`；`assign_summary_template(project, template, actor)`；`export_summary_workbook(project) -> bytes`；`export_raw_zip(project, subject_id | None) -> bytes`；`export_issue_workbook(project) -> bytes`。

- [ ] **Step 1: 写模板结构保持失败测试**

```python
from io import BytesIO
from zipfile import ZipFile
import pytest
from openpyxl import load_workbook
from apps.reporting.tests.workbook_helpers import find_employee_row
from apps.reporting.services.summary import (
    assign_summary_template,
    export_summary_workbook,
    register_summary_template,
)
from apps.reporting.services.raw import export_raw_zip


@pytest.mark.django_db
def test_summary_template_is_versioned_and_assigned_before_project_prepare(draft_project, template_bytes, hr_admin):
    template = register_summary_template(template_bytes, "summary-template.xlsx", hr_admin)
    assign_summary_template(draft_project, template, hr_admin)

    draft_project.refresh_from_db()
    assert draft_project.summary_template_id == template.id
    assert template.sha256
    assert template.version == 1


@pytest.mark.django_db
def test_summary_export_preserves_sheets_dimensions_styles_and_formulas(project_results, template_bytes):
    before = load_workbook(BytesIO(template_bytes))
    result = export_summary_workbook(project_results.project)
    after = load_workbook(BytesIO(result))

    assert after.sheetnames == before.sheetnames
    assert after["Sheet1"].max_column == before["Sheet1"].max_column
    assert after["Sheet1"].column_dimensions["A"].width == before["Sheet1"].column_dimensions["A"].width
    assert after["Sheet2"]["A1"]._style == before["Sheet2"]["A1"]._style
    assert after["Sheet3"]["A1"].value == before["Sheet3"]["A1"].value
```

- [ ] **Step 2: 写不完整结果失败测试**

```python
def test_incomplete_employee_exports_blank_total_and_explicit_status(incomplete_result, template_bytes):
    workbook = load_workbook(BytesIO(export_summary_workbook(incomplete_result.project)))

    row = find_employee_row(workbook["Sheet1"], incomplete_result.subject.employee_no)
    assert workbook["Sheet1"][f"L{row}"].value is None
    assert workbook["Sheet1"][f"M{row}"].value == "数据不完整"


@pytest.mark.django_db
def test_raw_export_is_partitioned_by_subject(project_results):
    subject = project_results.subject
    archive = ZipFile(BytesIO(export_raw_zip(project_results.project, subject.public_id)))

    names = archive.namelist()
    assert "manifest.json" in names
    assert any(name.startswith(f"{subject.public_id}/") and name.endswith(".xlsx") for name in names)
    assert all(name == "manifest.json" or name.startswith(f"{subject.public_id}/") for name in names)
```

- [ ] **Step 3: 运行导出测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/reporting/tests -v`

Expected: FAIL，因为导出服务尚不存在。

- [ ] **Step 4: 实现受控模板版本、项目绑定和汇总映射**

`SummaryWorkbookTemplate` 保存不可变版本号、私有存储路径、文件 SHA-256、结构签名、启用状态、创建人和创建时间，不把上传文件放进 Git 或公开静态目录。`register_summary_template()` 只接受 `.xlsx` 和 10MB 以内文件，在内存中验证已批准的工作表、表头和目标列后再写私有存储；同名后续上传创建新版本。`assign_summary_template()` 只允许修改 `draft` 项目；Task 6 的 `prepare_project()` 在最终系统中要求已绑定启用模板，并把模板版本和 SHA-256 写入项目快照。

`export_summary_workbook()` 从项目绑定且 SHA-256 与快照一致的模板读取字节，复制到内存，按已批准列映射写入值：员工编号、被评价人、邮箱、部门、上级均分、同部门均分、跨部门均分、总分和状态。只修改目标单元格的 `.value`；禁止调用 `create_sheet()`、删除工作表、调整列宽或写公式。输出前再次比较工作表名、维度、合并单元格、列宽、样式 ID 和公式，失败则抛出 `TemplateStructureError`，不返回半成品。
在 `apps/reporting/tests/workbook_helpers.py` 实现 `find_employee_row()`；在 `conftest.py` 提供内存 `template_bytes`，并让 `project_results` 和 `incomplete_result` 的项目都绑定由该字节注册的汇总模板版本。

- [ ] **Step 5: 实现原始和异常导出**

原始 ZIP 按 `被评价人 UUID/评价人_<评价人姓名>_被评价人_<被评价人姓名>.xlsx` 输出每份已提交网页表单；姓名只用于 HR 下载包内的文件名，并剔除路径保留字符，URL 和服务器公开路径仍只使用 UUID。工作簿固定包含被评价人、评价关系、评价项、快照描述、分数、可选备注和提交时间，并附项目清单；按 `subject_id` 导出时 ZIP 中只能出现该被评价人的目录。文件内容来自项目与提交快照，不读取当前模板；普通员工无导出权限。异常工作簿列出错误码、项目、被评价人员工编号、关系类型、任务状态和建议处理方式，不包含 token、Cookie 或企业微信 Secret。

- [ ] **Step 6: 运行导出测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations reporting evaluations && uv run python manage.py migrate && uv run pytest apps/reporting/tests -v`

Expected: 模板结构、空总分、按员工筛选、ZIP 路径、异常行和 HR 权限测试全部 PASS。

- [ ] **Step 7: 提交导出模块**

```bash
git add MAIN/apps/reporting MAIN/apps/evaluations/models/projects.py MAIN/apps/evaluations/services/projects.py MAIN/templates/hr/reporting MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: export template-faithful HR evaluation reports"
```

---

### Task 12: HR 工作台、权限和审计日志

**Files:**
- Create: `MAIN/apps/audit/models.py`
- Create: `MAIN/apps/audit/services.py`
- Create: `MAIN/apps/audit/middleware.py`
- Create: `MAIN/apps/audit/views.py`
- Create: `MAIN/apps/audit/urls.py`
- Create: `MAIN/apps/core/permissions.py`
- Create: `MAIN/templates/hr/base.html`
- Create: `MAIN/templates/hr/dashboard.html`
- Create: `MAIN/templates/hr/audit/list.html`
- Modify: `MAIN/apps/roster/views.py`
- Modify: `MAIN/apps/evaluations/views/projects.py`
- Modify: `MAIN/apps/reporting/views.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/audit/tests/test_audit_log.py`
- Test: `MAIN/apps/audit/tests/test_hr_permissions.py`

**Interfaces:**
- Consumes: Django Group、各业务服务和已登录用户。
- Produces: `require_hr_role(*roles)`；`record_audit(actor, action, target, changes)`；HR 首页和审计查询页。

- [ ] **Step 1: 写权限矩阵失败测试**

```python
import pytest


@pytest.mark.django_db
@pytest.mark.parametrize("role,path,expected", [
    ("EVALUATOR", "/hr/", 403),
    ("HR_OPERATOR", "/hr/", 200),
    ("HR_OPERATOR", "/hr/audit/", 403),
    ("HR_ADMIN", "/hr/audit/", 200),
])
def test_hr_role_matrix(client, user_with_role, role, path, expected):
    client.force_login(user_with_role(role))
    assert client.get(path).status_code == expected
```

- [ ] **Step 2: 写审计脱敏和不可修改失败测试**

```python
import json
import pytest
from apps.audit.models import AuditLogImmutableError
from apps.audit.services import record_audit


@pytest.mark.django_db
def test_audit_log_redacts_secrets_and_cannot_be_changed(hr_admin):
    event = record_audit(hr_admin, "email_config.update", "system", {"smtp_password": "secret-value"})

    assert "secret-value" not in json.dumps(event.change_summary, ensure_ascii=False)
    assert event.change_summary["smtp_password"] == "[REDACTED]"
    event.change_summary = "tampered"
    with pytest.raises(AuditLogImmutableError):
        event.save()
```

- [ ] **Step 3: 运行权限与审计测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/audit/tests -v`

Expected: FAIL，因为角色装饰器、审计模型和 HR 页面尚不存在。

- [ ] **Step 4: 实现角色校验和审计服务**

```python
from collections.abc import Mapping
from functools import wraps
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


SENSITIVE_PARTS = ("password", "secret", "token", "authorization", "cookie")


def sanitize_changes(value):
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if any(part in key.lower() for part in SENSITIVE_PARTS)
            else sanitize_changes(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_changes(item) for item in value]
    return value


def require_hr_role(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.groups.filter(name__in=allowed_roles).exists():
                raise PermissionDenied
            return view(request, *args, **kwargs)
        return wrapped
    return decorator
```

`AuditLog.save()` 只允许首次插入；自定义 `AuditLogQuerySet.update()` 和 `delete()` 均抛出 `AuditLogImmutableError`，实例 `delete()` 同样拒绝，避免通过 ORM 批量绕过；普通管理页面不注册删除操作。管理员查看审计时按时间、操作者和动作筛选，具体评分不写入摘要。未登录访问 HR 页面跳转到登录，已登录但角色不符明确返回 403。

- [ ] **Step 5: 组装 HR 工作台**

首页显示员工数、模板状态、项目状态、待完成任务和异常数量。所有新增、修改、导入确认、项目发送、重发、截止、汇总和导出视图在事务成功后调用 `record_audit()`。

- [ ] **Step 6: 运行权限与审计测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations audit && uv run python manage.py migrate && uv run pytest apps/audit/tests -v`

Expected: 角色矩阵、后端权限、脱敏、不可修改和关键操作审计测试全部 PASS。

- [ ] **Step 7: 提交 HR 工作台和审计**

```bash
git add MAIN/apps/audit MAIN/apps/core/permissions.py MAIN/templates/hr MAIN/apps/roster/views.py MAIN/apps/evaluations/views/projects.py MAIN/apps/reporting/views.py MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: enforce HR permissions and immutable audit logs"
```

---

### Task 13: 数据保留、截止处理和运行管理命令

**Files:**
- Create: `MAIN/apps/core/models.py`
- Create: `MAIN/apps/core/checks.py`
- Create: `MAIN/apps/evaluations/management/commands/close_expired_projects.py`
- Create: `MAIN/apps/notifications/management/commands/retry_notifications.py`
- Create: `MAIN/apps/core/management/commands/check_operational_readiness.py`
- Modify: `MAIN/apps/notifications/services.py`
- Modify: `MAIN/tests/factories.py`
- Modify: `MAIN/tests/conftest.py`
- Test: `MAIN/apps/core/tests/test_retention_gate.py`
- Test: `MAIN/apps/evaluations/tests/test_close_expired_projects.py`
- Test: `MAIN/apps/notifications/tests/test_retry_notifications.py`

**Interfaces:**
- Consumes: 项目、任务、通知日志和系统设置。
- Produces: `SystemSetting.data_retention_days`；发送前生产门禁；三个可由计划任务调用的幂等管理命令。

- [ ] **Step 1: 写生产门禁失败测试**

```python
import pytest
from apps.evaluations.services.projects import prepare_project
from apps.notifications.services import launch_project, ProjectLaunchBlocked


@pytest.mark.django_db
def test_production_launch_requires_retention_owner_and_incident_contact(
    settings, draft_project, hr_admin, fake_wecom,
):
    prepare_project(draft_project, hr_admin)
    settings.APP_ENV = "production"

    with pytest.raises(ProjectLaunchBlocked, match="生产运行配置不完整"):
        launch_project(draft_project, hr_admin, notifier=fake_wecom)
```

- [ ] **Step 2: 写截止和重试幂等失败测试**

```python
@pytest.mark.django_db
def test_close_expired_projects_marks_pending_tasks_without_touching_submissions(expired_active_project):
    call_command("close_expired_projects")
    call_command("close_expired_projects")

    expired_active_project.refresh_from_db()
    assert expired_active_project.status == "closed"
    assert expired_active_project.tasks.filter(status="not_submitted").count() == 2
    assert expired_active_project.tasks.filter(status="submitted").count() == 1
```

- [ ] **Step 3: 运行运行保障测试并确认 RED**

Run: `cd MAIN && uv run pytest apps/core/tests/test_retention_gate.py apps/evaluations/tests/test_close_expired_projects.py apps/notifications/tests/test_retry_notifications.py -v`

Expected: FAIL，因为系统设置和管理命令尚不存在。

- [ ] **Step 4: 实现系统设置和门禁**

`SystemSetting` 采用单例记录 `data_retention_days`、`business_owner`、`incident_contact` 和 `updated_by`。生产 `launch_project()` 在任何状态变更或外部通知前验证三项均已配置；开发和测试环境仍使用明确的测试设置，不读取生产值。

- [ ] **Step 5: 实现幂等管理命令**

`close_expired_projects` 关闭已过截止时间的活动项目并标记未提交任务；`retry_notifications` 只重试稳定错误码允许、未超过三次且仍未截止的通知；`check_operational_readiness` 检查数据库、保留设置、通知配置、证书剩余天数输入和最新备份时间，任一失败返回非零退出码。

- [ ] **Step 6: 运行运行保障测试并确认 GREEN**

Run: `cd MAIN && uv run python manage.py makemigrations core && uv run python manage.py migrate && uv run pytest apps/core/tests/test_retention_gate.py apps/evaluations/tests/test_close_expired_projects.py apps/notifications/tests/test_retry_notifications.py -v`

Expected: 生产门禁、截止幂等、通知重试限制和 readiness 退出码测试全部 PASS。

- [ ] **Step 7: 提交运行管理功能**

```bash
git add MAIN/apps/core MAIN/apps/evaluations/management MAIN/apps/notifications/management MAIN/apps/notifications/services.py MAIN/tests/factories.py MAIN/tests/conftest.py
git commit -m "feat: add production readiness and lifecycle commands"
```

---

### Task 14: 容器部署、HTTPS、备份和回滚运行手册

**Files:**
- Create: `MAIN/deploy/Dockerfile`
- Create: `MAIN/deploy/compose.staging.yml`
- Create: `MAIN/deploy/compose.production.yml`
- Create: `MAIN/deploy/nginx.conf`
- Create: `MAIN/deploy/backup.sh`
- Create: `MAIN/deploy/restore.sh`
- Create: `MAIN/deploy/runbooks/deploy.md`
- Create: `MAIN/deploy/runbooks/rollback.md`
- Create: `MAIN/deploy/runbooks/backup-restore.md`
- Create: `MAIN/deploy/runbooks/pilot.md`
- Test: `MAIN/tests/test_deploy_config.py`

**Interfaces:**
- Consumes: Task 1 的生产设置、Task 13 的 readiness 命令和外部注入的生产环境变量。
- Produces: 可解析的 staging/production Compose 配置、仅暴露 80/443 的 Nginx、加密异地备份脚本、逐步部署和回滚手册。

- [ ] **Step 1: 写部署配置失败测试**

```python
from pathlib import Path
import yaml


def test_production_database_has_no_host_port_and_app_has_healthcheck():
    compose = yaml.safe_load(Path("deploy/compose.production.yml").read_text(encoding="utf-8"))

    assert "ports" not in compose["services"]["db"]
    assert compose["services"]["web"]["healthcheck"]["test"] == [
        "CMD", "python", "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/', timeout=5)",
    ]
    assert compose["services"]["nginx"]["ports"] == ["80:80", "443:443"]
```

将 `PyYAML>=6,<7` 加入开发依赖并更新锁文件。

- [ ] **Step 2: 运行部署测试并确认 RED**

Run: `cd MAIN && uv lock && uv run pytest tests/test_deploy_config.py -v`

Expected: FAIL，因为部署文件尚不存在。

- [ ] **Step 3: 实现最小生产容器结构**

`Dockerfile` 使用 Python 3.12 slim，多阶段安装锁定依赖，以非 root 用户运行 Gunicorn。生产 Compose 包含 `web`、`db`、`nginx` 三个服务；数据库只在内部网络监听，密码从服务器环境注入；应用健康检查使用镜像已有的 Python 标准库，数据库使用 `pg_isready`，不为健康检查额外安装 curl。Nginx 强制 HTTPS、安全响应头和合理上传限制。

- [ ] **Step 4: 实现备份和恢复脚本**

`backup.sh` 使用 `pg_dump --format=custom`，生成 SHA-256，使用服务器提供的备份公钥加密后上传到独立存储；成功后才更新 `latest-backup.json`。`restore.sh` 要求显式目标数据库和备份文件，先验证校验和，再恢复到空数据库，禁止默认指向生产库。

- [ ] **Step 5: 编写部署和回滚手册**

部署手册固定顺序：只读盘点 → 备份 → 预发布迁移 → 自动测试 → 操作者批准 → 生产迁移 → 健康检查 → 业务抽查。回滚手册区分应用镜像回切和数据库恢复，列出触发条件、负责人、命令和验证结果。试点手册规定 10～20 人、测试项目、验收表和停止条件。

- [ ] **Step 6: 验证部署配置并确认 GREEN**

Run: `cd MAIN && uv run pytest tests/test_deploy_config.py -v && docker compose -f deploy/compose.staging.yml config && docker compose -f deploy/compose.production.yml config && uv run python manage.py check --deploy --settings=config.settings.production`

Expected: 部署测试 PASS，两份 Compose 可解析，生产检查不出现未处理安全警告。

- [ ] **Step 7: 提交部署和运维文件**

```bash
git add MAIN/deploy MAIN/tests/test_deploy_config.py MAIN/pyproject.toml MAIN/uv.lock
git commit -m "feat: add gated cloud deployment and recovery tooling"
```

---

### Task 15: 端到端验收、安全检查和试点发布包

**Files:**
- Create: `MAIN/tests/e2e/test_employee_journey.py`
- Create: `MAIN/tests/e2e/test_hr_journey.py`
- Create: `MAIN/tests/e2e/test_forwarded_link.py`
- Create: `MAIN/tests/e2e/helpers.py`
- Create: `MAIN/tests/e2e/conftest.py`
- Create: `MAIN/tests/test_security_boundaries.py`
- Create: `MAIN/scripts/create_sanitized_pilot.py`
- Create: `MAIN/docs/acceptance/pilot-checklist.md`
- Create: `MAIN/docs/acceptance/release-evidence.md`
- Modify: `MAIN/README.md`

**Interfaces:**
- Consumes: Tasks 1～14 的完整系统和预发布环境。
- Produces: 可重复的脱敏试点数据、浏览器端到端测试、发布证据模板和最终试点验收清单。

- [ ] **Step 1: 写员工端到端失败测试**

```python
from tests.e2e.helpers import complete_remaining_answers, login_as_wecom_user


def test_employee_can_login_save_draft_submit_once_and_cannot_open_forwarded_task(live_server, page, pilot_data):
    login_as_wecom_user(page, live_server, pilot_data.evaluator_a.wecom_userid)
    page.goto(f"{live_server.url}/tasks/")
    page.get_by_text(pilot_data.subject_a.name).click()
    page.get_by_label("责任心与执行力").check("5")
    page.get_by_role("button", name="保存草稿").click()
    page.reload()
    assert page.get_by_label("责任心与执行力").is_checked()
    complete_remaining_answers(page, score="4")
    page.get_by_role("button", name="正式提交").click()
    page.get_by_role("button", name="确认提交").click()
    assert page.get_by_text("提交成功").is_visible()

    login_as_wecom_user(page, live_server, pilot_data.evaluator_b.wecom_userid)
    page.goto(pilot_data.evaluator_a_task_url)
    assert page.get_by_text("页面不存在或无权访问").is_visible()
```

- [ ] **Step 2: 写 HR 全链路失败测试**

```python
from tests.e2e.helpers import (
    assert_workbook_matches_template,
    create_prepare_and_launch_project,
    import_relationships_and_confirm,
    import_roster_and_confirm,
    login_as_hr_admin,
    seed_completed_submissions,
)


def test_hr_can_import_dispatch_monitor_aggregate_and_export(
    live_server, page, pilot_files, pilot_data,
):
    login_as_hr_admin(page, live_server, pilot_data.hr_admin)
    import_roster_and_confirm(page, pilot_files.roster)
    import_relationships_and_confirm(page, pilot_files.relationships, mode="replace")
    project_id = create_prepare_and_launch_project(page, name="脱敏试点评价")
    seed_completed_submissions(project_id)
    page.goto(f"{live_server.url}/hr/projects/{project_id}/results/")
    page.get_by_role("button", name="重新汇总").click()
    assert page.get_by_text("数据不完整").is_visible()
    with page.expect_download() as download:
        page.get_by_role("button", name="导出数据汇总表").click()
    assert_workbook_matches_template(download.value.path(), pilot_files.summary_template)
```

- [ ] **Step 3: 运行端到端测试并确认 RED**

Run: `cd MAIN && uv run playwright install chromium && uv run pytest tests/e2e -v`

Expected: 若任何路由、权限、浏览器交互或导出链路未完成则 FAIL；修复必须回到对应任务增加最小测试和实现。

- [ ] **Step 4: 实现 E2E 辅助接口、脱敏 fixtures 和试点生成器**

`tests/e2e/helpers.py` 必须定义并由上述测试显式导入以下接口，不允许依赖未声明的全局 helper：

```python
from pathlib import Path
from urllib.parse import urljoin, urlparse
from django.conf import settings
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore
from openpyxl import load_workbook
from apps.evaluations.models.projects import EvaluationProject
from apps.evaluations.services.submissions import submit_task
from apps.roster.models import Employee


def _install_session(page, live_server, user) -> None:
    session = SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()
    session.save()
    host = urlparse(live_server.url).hostname
    page.context.add_cookies([{
        "name": settings.SESSION_COOKIE_NAME, "value": session.session_key,
        "domain": host, "path": "/", "httpOnly": True, "sameSite": "Lax",
    }])
    page.goto(f"{live_server.url}/")


def _goto(page, path: str) -> None:
    page.goto(urljoin(page.url, path))


def login_as_wecom_user(page, live_server, wecom_userid: str) -> None:
    employee = Employee.objects.select_related("user").get(wecom_userid=wecom_userid)
    _install_session(page, live_server, employee.user)


def login_as_hr_admin(page, live_server, user) -> None:
    _install_session(page, live_server, user)


def complete_remaining_answers(page, score: str) -> None:
    for item in page.locator("[data-evaluation-item]").all():
        if item.locator("input:checked").count() == 0:
            item.locator(f'input[type="radio"][value="{score}"]').check()


def import_roster_and_confirm(page, path: Path) -> None:
    _goto(page, "/hr/roster/import/")
    page.get_by_label("花名册文件").set_input_files(path)
    page.get_by_role("button", name="预检").click()
    page.get_by_label("新增导入").check()
    page.get_by_label("跳过重复项目").check()
    page.get_by_role("button", name="确认导入").click()


def import_relationships_and_confirm(page, path: Path, mode: str) -> None:
    _goto(page, "/hr/relationships/import/")
    page.get_by_label("协作关系文件").set_input_files(path)
    page.get_by_role("button", name="预检").click()
    page.get_by_label("覆盖导入" if mode == "replace" else "新增导入").check()
    page.get_by_label("更新重复项目").check()
    page.get_by_role("button", name="确认导入").click()


def create_prepare_and_launch_project(page, name: str) -> str:
    _goto(page, "/hr/projects/new/")
    page.get_by_label("项目名称").fill(name)
    page.get_by_role("button", name="创建并预览").click()
    page.get_by_role("button", name="冻结任务").click()
    page.get_by_role("button", name="正式下发").click()
    return page.locator("[data-project-id]").get_attribute("data-project-id")


def seed_completed_submissions(project_public_id: str) -> None:
    project = EvaluationProject.objects.get(public_id=project_public_id)
    for task in project.tasks.order_by("id")[:-1]:
        subject = project.subjects.get(employee=task.subject)
        answers = {item["snapshot_id"]: 4 for item in subject.template_snapshot["items"]}
        submit_task(task, task.evaluator.user, answers, f"e2e-{task.public_id}")


def assert_workbook_matches_template(result_path: Path, template_path: Path) -> None:
    before = load_workbook(template_path, data_only=False)
    after = load_workbook(result_path, data_only=False)
    assert after.sheetnames == before.sheetnames
    for name in before.sheetnames:
        assert after[name].max_column == before[name].max_column
        assert after[name].merged_cells.ranges == before[name].merged_cells.ranges
```

Task 7 的评价项容器必须输出 `data-evaluation-item`；Task 6 的项目详情根节点必须输出不可猜测 UUID 的 `data-project-id`；Task 4 和 Task 6/10 的按钮与标签使用上述稳定中文名称，保证 E2E 测试不依赖 CSS 类。`tests/e2e/conftest.py` 使用测试工厂创建 `pilot_data`，并用 `tmp_path` 与 `tests/helpers.py` 写出虚构的 `pilot_files.roster`、`pilot_files.relationships` 和 `pilot_files.summary_template`，不得读取桌面真实文件。

`create_sanitized_pilot.py` 固定生成 20 名虚构员工、3 个部门、4 个员工类别、3 类评价关系、2 个模板、1 个项目和完整/不完整两类提交；脚本启动时断言 `APP_ENV` 为 `local` 或 `test`，并拒绝生产数据库主机。`pilot-checklist.md` 逐项记录企业微信回调、任务隔离、草稿、单次提交、通知、汇总、模板导出、审计、备份和恢复结果。

- [ ] **Step 5: 执行完整质量门禁并确认 GREEN**

Run: `cd MAIN && uv run pytest -v --cov=apps --cov-report=term-missing && uv run python manage.py check --deploy --settings=config.settings.production && uv run pip-audit && git diff --check`

Expected: 全部测试 PASS；权限、评分和提交服务分支覆盖率 100%，整体覆盖率至少 90%；Django 安全检查无未处理警告；依赖无已知高危漏洞；Git diff 无格式错误。

- [ ] **Step 6: 在预发布执行恢复和试点门禁**

严格按 `deploy/runbooks/backup-restore.md` 将最新备份恢复到空的验证数据库，运行 `check_operational_readiness`，再由 10～20 名试点人员完成验收。任何任务越权、重复提交、总分误算、模板结构改变或备份无法恢复都阻断生产上线。

- [ ] **Step 7: 记录发布证据并提交试点包**

`release-evidence.md` 记录提交 SHA、镜像摘要、迁移清单、测试结果、漏洞检查、恢复演练、HR 验收人和回滚镜像，不记录 Secret 或员工数据。

```bash
git add MAIN/tests/e2e MAIN/tests/test_security_boundaries.py MAIN/scripts/create_sanitized_pilot.py MAIN/docs/acceptance MAIN/README.md
git commit -m "test: verify production HR evaluation workflow"
```

---

## Final Execution Order

1. Task 1～3：工程基础、人员模型和企业微信认证。
2. Task 4～6：花名册关系、模板和项目任务。
3. Task 7～9：员工填写、提交和评分汇总。
4. Task 10～13：通知、导出、审计和运行保障。
5. Task 14：只完成部署文件和预发布验证，不接触生产服务器。
6. Task 15：全链路验收；生产操作必须另行取得服务器、域名、企业微信和发布授权。

每个任务完成后均需运行该任务列出的测试并审查 `git diff`。不得为了让测试通过而降低权限、放宽数据完整性或跳过生产门禁。
