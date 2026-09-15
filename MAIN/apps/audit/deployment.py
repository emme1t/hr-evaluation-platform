EXPECTED_AUDIT_TRIGGERS = {
    "audit_auditlog_no_delete",
    "audit_auditlog_no_truncate",
    "audit_auditlog_no_update",
}


class AuditDeploymentError(RuntimeError):
    pass


def validate_audit_postgresql_evidence(
    evidence, *, require_connection_identity=True
):
    table_oid = evidence.get("table_oid")
    owner = evidence.get("owner_role")
    runtime = evidence.get("runtime_role")
    if not isinstance(table_oid, int) or table_oid <= 0:
        raise AuditDeploymentError("审计表 OID 无效")
    if not evidence.get("runtime_role_exists") or not runtime:
        raise AuditDeploymentError("审计运行角色不存在")
    if not owner or owner == runtime:
        raise AuditDeploymentError("审计运行角色必须与表所有者分离")
    if require_connection_identity and (
        evidence.get("current_user") != runtime
        or evidence.get("session_user") != runtime
    ):
        raise AuditDeploymentError("数据库连接未使用配置的审计运行角色")
    sequence_oid = evidence.get("sequence_oid")
    if not isinstance(sequence_oid, int) or sequence_oid <= 0:
        raise AuditDeploymentError("审计主键序列不存在")
    if evidence.get("sequence_owner_role") != owner:
        raise AuditDeploymentError("审计主键序列所有者与表所有者不一致")
    if (
        evidence.get("sequence_table_oid") != table_oid
        or evidence.get("sequence_column_name") != "id"
    ):
        raise AuditDeploymentError("审计主键序列未绑定到审计表 id 列")
    expected_runtime = {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }
    if evidence.get("runtime_privileges") != expected_runtime:
        raise AuditDeploymentError("审计运行角色权限不符合只读加追加契约")
    if evidence.get("runtime_sequence_privileges") != {
        "USAGE": True,
        "SELECT": True,
        "UPDATE": False,
    }:
        raise AuditDeploymentError("审计运行角色序列权限不符合追加契约")
    if evidence.get("public_mutation_privileges") != {
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }:
        raise AuditDeploymentError("PUBLIC 仍持有审计变更权限")
    if evidence.get("public_sequence_privileges") != {
        "USAGE": False,
        "SELECT": False,
        "UPDATE": False,
    }:
        raise AuditDeploymentError("PUBLIC 仍持有审计序列权限")
    triggers = evidence.get("triggers") or []
    if {row[0] for row in triggers} != EXPECTED_AUDIT_TRIGGERS:
        raise AuditDeploymentError("审计阻断触发器不完整")
    if any(row[1] != table_oid or row[2] not in {"O", "A"} for row in triggers):
        raise AuditDeploymentError("审计触发器关系 OID 或启用状态无效")
    return evidence


def verify_audit_postgresql_deployment(
    connection, runtime_role, *, require_connection_identity=True
):
    if connection.vendor != "postgresql":
        raise AuditDeploymentError("审计部署验证必须在 PostgreSQL 运行")
    if not isinstance(runtime_role, str) or not runtime_role.strip():
        raise AuditDeploymentError("未配置 AUDIT_RUNTIME_DB_ROLE")
    runtime_role = runtime_role.strip()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.oid,
                   pg_get_userbyid(c.relowner),
                   runtime.rolname,
                   runtime.oid IS NOT NULL,
                   current_user,
                   session_user,
                   CASE WHEN runtime.oid IS NULL THEN FALSE ELSE has_table_privilege(runtime.rolname, c.oid, 'SELECT') END,
                   CASE WHEN runtime.oid IS NULL THEN FALSE ELSE has_table_privilege(runtime.rolname, c.oid, 'INSERT') END,
                   CASE WHEN runtime.oid IS NULL THEN FALSE ELSE has_table_privilege(runtime.rolname, c.oid, 'UPDATE') END,
                   CASE WHEN runtime.oid IS NULL THEN FALSE ELSE has_table_privilege(runtime.rolname, c.oid, 'DELETE') END,
                   CASE WHEN runtime.oid IS NULL THEN FALSE ELSE has_table_privilege(runtime.rolname, c.oid, 'TRUNCATE') END,
                   has_table_privilege('public', c.oid, 'UPDATE'),
                   has_table_privilege('public', c.oid, 'DELETE'),
                   has_table_privilege('public', c.oid, 'TRUNCATE')
            FROM pg_class c
            LEFT JOIN pg_roles runtime ON runtime.rolname = %s
            WHERE c.oid = to_regclass('audit_auditlog')
            """,
            [runtime_role],
        )
        row = cursor.fetchone()
        if row is None:
            raise AuditDeploymentError("审计表不存在")
        cursor.execute(
            """
            SELECT sequence_rel.oid,
                   pg_get_userbyid(sequence_rel.relowner),
                   dependency.refobjid,
                   attribute.attname,
                   CASE WHEN runtime.oid IS NULL OR sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege(runtime.rolname, sequence_rel.oid, 'USAGE') END,
                   CASE WHEN runtime.oid IS NULL OR sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege(runtime.rolname, sequence_rel.oid, 'SELECT') END,
                   CASE WHEN runtime.oid IS NULL OR sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege(runtime.rolname, sequence_rel.oid, 'UPDATE') END,
                   CASE WHEN sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege('public', sequence_rel.oid, 'USAGE') END,
                   CASE WHEN sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege('public', sequence_rel.oid, 'SELECT') END,
                   CASE WHEN sequence_rel.oid IS NULL THEN FALSE ELSE has_sequence_privilege('public', sequence_rel.oid, 'UPDATE') END
            FROM pg_class table_rel
            LEFT JOIN pg_class sequence_rel
              ON sequence_rel.oid = pg_get_serial_sequence('audit_auditlog', 'id')::regclass
            LEFT JOIN pg_depend dependency
              ON dependency.classid = 'pg_class'::regclass
             AND dependency.objid = sequence_rel.oid
             AND dependency.refclassid = 'pg_class'::regclass
             AND dependency.refobjid = table_rel.oid
             AND dependency.deptype IN ('a', 'i')
            LEFT JOIN pg_attribute attribute
              ON attribute.attrelid = dependency.refobjid
             AND attribute.attnum = dependency.refobjsubid
            LEFT JOIN pg_roles runtime ON runtime.rolname = %s
            WHERE table_rel.oid = %s
            """,
            [runtime_role, row[0]],
        )
        sequence_row = cursor.fetchone()
        if sequence_row is None:
            raise AuditDeploymentError("审计主键序列不存在")
        cursor.execute(
            """
            SELECT tgname, tgrelid, tgenabled
            FROM pg_trigger
            WHERE tgrelid = %s AND NOT tgisinternal
            ORDER BY tgname
            """,
            [row[0]],
        )
        triggers = cursor.fetchall()
    evidence = {
        "table_oid": row[0],
        "owner_role": row[1],
        "runtime_role": runtime_role,
        "runtime_role_exists": row[3],
        "current_user": row[4],
        "session_user": row[5],
        "sequence_oid": sequence_row[0],
        "sequence_owner_role": sequence_row[1],
        "sequence_table_oid": sequence_row[2],
        "sequence_column_name": sequence_row[3],
        "runtime_privileges": dict(
            zip(
                ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"),
                row[6:11],
                strict=True,
            )
        ),
        "public_mutation_privileges": dict(
            zip(("UPDATE", "DELETE", "TRUNCATE"), row[11:14], strict=True)
        ),
        "runtime_sequence_privileges": dict(
            zip(("USAGE", "SELECT", "UPDATE"), sequence_row[4:7], strict=True)
        ),
        "public_sequence_privileges": dict(
            zip(("USAGE", "SELECT", "UPDATE"), sequence_row[7:10], strict=True)
        ),
        "triggers": list(triggers),
    }
    return validate_audit_postgresql_evidence(
        evidence, require_connection_identity=require_connection_identity
    )


def _qualified_relation(connection, schema_name, relation_name):
    return ".".join(
        (
            connection.ops.quote_name(schema_name),
            connection.ops.quote_name(relation_name),
        )
    )


def configure_audit_postgresql_acl(connection, runtime_role):
    if connection.vendor != "postgresql":
        raise AuditDeploymentError("审计权限配置必须在 PostgreSQL 运行")
    if not isinstance(runtime_role, str) or not runtime_role.strip():
        raise AuditDeploymentError("未配置 AUDIT_RUNTIME_DB_ROLE")
    runtime_role = runtime_role.strip()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT table_ns.nspname,
                   table_rel.relname,
                   table_rel.oid,
                   pg_get_userbyid(table_rel.relowner),
                   sequence_ns.nspname,
                   sequence_rel.relname,
                   sequence_rel.oid,
                   pg_get_userbyid(sequence_rel.relowner),
                   dependency.refobjid,
                   attribute.attname,
                   runtime.oid IS NOT NULL,
                   current_user,
                   session_user
            FROM pg_class table_rel
            JOIN pg_namespace table_ns ON table_ns.oid = table_rel.relnamespace
            LEFT JOIN pg_class sequence_rel
              ON sequence_rel.oid = pg_get_serial_sequence('audit_auditlog', 'id')::regclass
            LEFT JOIN pg_namespace sequence_ns
              ON sequence_ns.oid = sequence_rel.relnamespace
            LEFT JOIN pg_depend dependency
              ON dependency.classid = 'pg_class'::regclass
             AND dependency.objid = sequence_rel.oid
             AND dependency.refclassid = 'pg_class'::regclass
             AND dependency.refobjid = table_rel.oid
             AND dependency.deptype IN ('a', 'i')
            LEFT JOIN pg_attribute attribute
              ON attribute.attrelid = dependency.refobjid
             AND attribute.attnum = dependency.refobjsubid
            LEFT JOIN pg_roles runtime ON runtime.rolname = %s
            WHERE table_rel.oid = to_regclass('audit_auditlog')
            """,
            [runtime_role],
        )
        row = cursor.fetchone()
        if row is None:
            raise AuditDeploymentError("审计表不存在")
        (
            table_schema,
            table_name,
            table_oid,
            owner_role,
            sequence_schema,
            sequence_name,
            sequence_oid,
            sequence_owner_role,
            sequence_table_oid,
            sequence_column_name,
            runtime_role_exists,
            current_user,
            session_user,
        ) = row
        if not runtime_role_exists:
            raise AuditDeploymentError("审计运行角色不存在")
        if not owner_role or owner_role == runtime_role:
            raise AuditDeploymentError("审计运行角色必须与表所有者分离")
        if current_user != owner_role or session_user != owner_role:
            raise AuditDeploymentError("审计权限配置必须由表所有者连接执行")
        if (
            not isinstance(sequence_oid, int)
            or sequence_oid <= 0
            or sequence_owner_role != owner_role
            or sequence_table_oid != table_oid
            or sequence_column_name != "id"
            or not sequence_schema
            or not sequence_name
        ):
            raise AuditDeploymentError("审计主键序列关联无效")
        table_relation = _qualified_relation(connection, table_schema, table_name)
        sequence_relation = _qualified_relation(
            connection, sequence_schema, sequence_name
        )
        quoted_role = connection.ops.quote_name(runtime_role)
        statements = [
            f"GRANT SELECT, INSERT ON TABLE {table_relation} TO {quoted_role}",
            f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {table_relation} FROM {quoted_role}",
            f"REVOKE UPDATE, DELETE, TRUNCATE ON TABLE {table_relation} FROM PUBLIC",
            f"GRANT USAGE, SELECT ON SEQUENCE {sequence_relation} TO {quoted_role}",
            f"REVOKE UPDATE ON SEQUENCE {sequence_relation} FROM {quoted_role}",
            f"REVOKE USAGE, SELECT, UPDATE ON SEQUENCE {sequence_relation} FROM PUBLIC",
        ]
        for statement in statements:
            cursor.execute(statement)
    return verify_audit_postgresql_deployment(
        connection, runtime_role, require_connection_identity=False
    )
