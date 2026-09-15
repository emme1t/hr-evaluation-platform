# 人事评价系统 · 恢复开发版

正式 Django 软件的独立源码仓库，包含人员与评价关系、模板、项目、员工填写、评分汇总、通知、报表和审计模块。

## 当前进度

- Task 9–12 已完成本地代码恢复及 portable/SQLite 阶段验收。
- Task 12 历史记录：792 passed，13 项 PostgreSQL 专项 skipped。
- 离线运行配置已实现；离线初始化、恢复和相关测试处于开发中，本仓库保留这些工作文件。
- 真实 PostgreSQL、浏览器完整业务流程、消息服务商、离线交付包和最终人工验收仍待完成。
- 当前版本为开发快照，生产发布状态为待验收。

## 目录

- `MAIN/apps/`：Django 业务应用、迁移与测试。
- `MAIN/config/`：本地、离线及生产配置。
- `MAIN/templates/`、`MAIN/static/`：页面和前端资源。
- `MAIN/docs/`：历史设计与实施计划，当前状态以本 README 和 docs/PROJECT_STATUS.md 为准。
- `docs/`：整理来源、进度和验证说明。

## 开发验证

安装 uv、Python 3.12 后执行：

```powershell
cd MAIN
uv sync --frozen --extra dev
uv run python manage.py check
uv run python -m pytest
```

默认测试配置使用本地 SQLite。PostgreSQL 专项需在独立测试数据库验证。测试数据应使用虚构资料。

## 文件管理

Git 管理源码、迁移、测试、依赖锁文件、脱敏配置样例和项目文档。
凭据、本机配置、数据库及其附属文件、员工数据、上传文件、导出、备份、依赖和构建缓存通过 `.gitignore` 排除。真实业务资料应存放在仓库之外。

整理来源及验证范围见 [项目状态](docs/PROJECT_STATUS.md)。
