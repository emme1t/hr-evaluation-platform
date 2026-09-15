from io import BytesIO
import struct
import zlib
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.roster.imports import (
    ImportPreviewError,
    preview_relationship_upload,
    preview_roster_upload,
)
from apps.roster.models import Employee
from tests.factories import create_category
from tests.helpers import (
    RELATIONSHIP_HEADERS,
    ROSTER_HEADERS,
    build_csv_bytes,
    build_relationship_workbook_bytes,
    build_xlsx_bytes,
)


def roster_row(index, category_code="TEST"):
    return (
        f"E{index:04d}",
        f"Employee {index}",
        f"e{index:04d}@example.test",
        "Test Center",
        "Test Team",
        category_code,
        f"wx_e{index:04d}",
    )


def append_archive_member(content, name, data=None, expanded_size=None):
    output = BytesIO(content)
    with ZipFile(output, "a", compression=ZIP_DEFLATED) as archive:
        if expanded_size is None:
            archive.writestr(name, data or b"")
        else:
            chunk = b"x" * 64 * 1024
            remaining = expanded_size
            with archive.open(name, "w") as member:
                while remaining:
                    part = chunk[: min(len(chunk), remaining)]
                    member.write(part)
                    remaining -= len(part)
    return output.getvalue()


def archive_member_offsets(content, name):
    encoded_name = name.encode("utf-8")
    offsets = {}
    for key, signature, fixed_size, name_length_offset in (
        ("local", b"PK\x03\x04", 30, 26),
        ("central", b"PK\x01\x02", 46, 28),
    ):
        cursor = 0
        while True:
            offset = content.find(signature, cursor)
            if offset < 0:
                raise AssertionError(f"missing {key} header for {name}")
            name_length = struct.unpack_from(
                "<H", content, offset + name_length_offset
            )[0]
            candidate = content[offset + fixed_size : offset + fixed_size + name_length]
            if candidate == encoded_name:
                offsets[key] = offset
                break
            cursor = offset + 4
    return offsets


def forge_member_identity_metadata(content, name, declared_content):
    forged = bytearray(content)
    offsets = archive_member_offsets(forged, name)
    declared_crc = zlib.crc32(declared_content)
    struct.pack_into("<I", forged, offsets["local"] + 14, declared_crc)
    struct.pack_into("<I", forged, offsets["local"] + 22, len(declared_content))
    struct.pack_into("<I", forged, offsets["central"] + 16, declared_crc)
    struct.pack_into("<I", forged, offsets["central"] + 24, len(declared_content))
    return bytes(forged)


def set_member_encrypted_flag(content, name):
    encrypted = bytearray(content)
    offsets = archive_member_offsets(encrypted, name)
    for offset, flag_offset in ((offsets["local"], 6), (offsets["central"], 8)):
        flags = struct.unpack_from("<H", encrypted, offset + flag_offset)[0]
        struct.pack_into("<H", encrypted, offset + flag_offset, flags | 0x1)
    return bytes(encrypted)


def archive_with_member_count(content, member_count):
    with ZipFile(BytesIO(content)) as archive:
        existing_count = len(archive.infolist())
    output = BytesIO(content)
    with ZipFile(output, "a", compression=ZIP_DEFLATED) as archive:
        for index in range(member_count - existing_count):
            archive.writestr(f"bounded/member-{index:04d}.txt", b"x")
    return output.getvalue()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "headers",
    [ROSTER_HEADERS + ("未知列",), ROSTER_HEADERS + ("姓名",)],
    ids=("unknown-column", "duplicate-column"),
)
def test_roster_preview_requires_exact_unique_headers(headers, hr_admin):
    content = build_csv_bytes(headers, [roster_row(1) + ("extra",)])

    with pytest.raises(ImportPreviewError, match="导入列必须完全匹配"):
        preview_roster_upload(content, "roster.csv", hr_admin)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "row",
    [roster_row(1)[:-1], roster_row(1) + ("unexpected",)],
    ids=("missing-cell", "extra-cell"),
)
def test_roster_preview_rejects_rows_that_do_not_match_fixed_columns(row, hr_admin):
    content = build_csv_bytes(ROSTER_HEADERS, [row])

    with pytest.raises(ImportPreviewError, match="每行必须严格包含固定列"):
        preview_roster_upload(content, "roster.csv", hr_admin)


@pytest.mark.django_db
def test_relationship_preview_rejects_more_than_500_rows(employee_set, hr_admin):
    rows = [("E001", "E002", "manager") for _ in range(501)]
    content = build_csv_bytes(RELATIONSHIP_HEADERS, rows)

    with pytest.raises(ImportPreviewError, match="最多 500 行"):
        preview_relationship_upload(content, "relations.csv", hr_admin)


@pytest.mark.django_db
def test_roster_preview_accepts_500_rows_with_fixed_3500_cells(hr_admin):
    create_category("TEST", "Test Category")
    content = build_csv_bytes(
        ROSTER_HEADERS, [roster_row(index) for index in range(500)]
    )

    batch = preview_roster_upload(content, "roster.csv", hr_admin)

    assert batch.valid_count == 500
    assert batch.issue_count == 0


@pytest.mark.django_db
def test_roster_preview_rejects_more_than_100_issues(employee_set, hr_admin):
    rows = [roster_row(index, category_code="MISSING") for index in range(101)]
    content = build_csv_bytes(ROSTER_HEADERS, rows)

    with pytest.raises(ImportPreviewError, match="异常行数不能超过 100"):
        preview_roster_upload(content, "roster.csv", hr_admin)


@pytest.mark.django_db
def test_roster_preview_accepts_exactly_100_issues(hr_admin):
    rows = [roster_row(index, category_code="MISSING") for index in range(100)]

    batch = preview_roster_upload(
        build_csv_bytes(ROSTER_HEADERS, rows), "roster.csv", hr_admin
    )

    assert batch.valid_count == 0
    assert batch.issue_count == 100


@pytest.mark.django_db
def test_xlsx_preview_rejects_excessive_decompressed_size(hr_admin):
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("xl/worksheets/sheet1.xml", b"x" * (20 * 1024 * 1024 + 1))

    with pytest.raises(ImportPreviewError, match="解压后不能超过 20MB"):
        preview_roster_upload(output.getvalue(), "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_xlsx_preview_rejects_actual_expansion_when_size_and_crc_are_forged(hr_admin):
    content = build_xlsx_bytes(ROSTER_HEADERS, [])
    bomb_name = "xl/ignored-bomb.bin"
    content = append_archive_member(
        content,
        bomb_name,
        expanded_size=20 * 1024 * 1024 + 1,
    )
    forged = forge_member_identity_metadata(content, bomb_name, b"x")

    with pytest.raises(ImportPreviewError, match="解压后不能超过 20MB"):
        preview_roster_upload(forged, "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_xlsx_preview_rejects_malformed_member_crc(hr_admin):
    member_name = "xl/ignored-invalid-crc.bin"
    content = append_archive_member(
        build_xlsx_bytes(ROSTER_HEADERS, []),
        member_name,
        data=b"actual member bytes",
    )
    forged = forge_member_identity_metadata(
        content,
        member_name,
        b"different contents!",
    )

    with pytest.raises(ImportPreviewError, match="文件无法解析"):
        preview_roster_upload(forged, "roster.xlsx", hr_admin)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("member_count", "accepted"),
    [(1000, True), (1001, False)],
    ids=("at-limit", "over-limit"),
)
def test_xlsx_preview_enforces_member_count_boundary(hr_admin, member_count, accepted):
    content = archive_with_member_count(
        build_xlsx_bytes(ROSTER_HEADERS, []), member_count
    )

    if accepted:
        assert preview_roster_upload(content, "roster.xlsx", hr_admin).valid_count == 0
    else:
        with pytest.raises(ImportPreviewError, match="文件项过多"):
            preview_roster_upload(content, "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_xlsx_preview_rejects_unsafe_member_path(hr_admin):
    content = append_archive_member(
        build_xlsx_bytes(ROSTER_HEADERS, []),
        "../ignored.xml",
        data=b"ignored",
    )

    with pytest.raises(ImportPreviewError, match="路径不安全"):
        preview_roster_upload(content, "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_xlsx_preview_rejects_encrypted_member(hr_admin):
    member_name = "xl/encrypted-ignored.bin"
    content = append_archive_member(
        build_xlsx_bytes(ROSTER_HEADERS, []), member_name, data=b"ignored"
    )
    content = set_member_encrypted_flag(content, member_name)

    with pytest.raises(ImportPreviewError, match="加密"):
        preview_roster_upload(content, "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_xlsx_preview_rejects_duplicate_member_names(hr_admin):
    member_name = "xl/duplicate-ignored.bin"
    content = append_archive_member(
        build_xlsx_bytes(ROSTER_HEADERS, []), member_name, data=b"first"
    )
    with pytest.warns(UserWarning, match="Duplicate name"):
        content = append_archive_member(content, member_name, data=b"second")

    with pytest.raises(ImportPreviewError, match="重复文件项"):
        preview_roster_upload(content, "roster.xlsx", hr_admin)


@pytest.mark.django_db
def test_issue_preview_is_paginated_to_50_rows(client, hr_admin, employee_set):
    rows = [roster_row(index, category_code="MISSING") for index in range(60)]
    batch = preview_roster_upload(build_csv_bytes(ROSTER_HEADERS, rows), "roster.csv", hr_admin)
    client.force_login(hr_admin)

    response = client.get(reverse("roster:import-preview", args=[batch.public_id]))

    assert response.status_code == 200
    assert len(response.context["issue_page"]) == 50
    assert response.context["issue_page"].has_next() is True
    assert "第 61 行" not in response.content.decode()


@pytest.mark.django_db
def test_roster_preview_query_count_is_bounded_for_500_rows(hr_admin):
    category = create_category("TEST", "Test Category")
    employees = [
        Employee(
            employee_no=f"E{index:04d}",
            name=f"Employee {index}",
            corporate_email=f"e{index:04d}@example.test",
            department_level_1="Test Center",
            department_level_2="Test Team",
            category=category,
            wecom_userid=f"wx_e{index:04d}",
        )
        for index in range(500)
    ]
    Employee.objects.bulk_create(employees)
    content = build_csv_bytes(ROSTER_HEADERS, [roster_row(index) for index in range(500)])

    with CaptureQueriesContext(connection) as queries:
        batch = preview_roster_upload(content, "roster.csv", hr_admin)

    assert batch.valid_count == 500
    assert len(queries) <= 12


@pytest.mark.django_db
def test_relationship_preview_query_count_is_bounded_for_500_rows(hr_admin):
    category = create_category("TEST", "Test Category")
    employees = [
        Employee(
            employee_no=f"E{index:04d}",
            name=f"Employee {index}",
            corporate_email=f"e{index:04d}@example.test",
            department_level_1="Test Center",
            department_level_2="Test Team",
            category=category,
        )
        for index in range(501)
    ]
    Employee.objects.bulk_create(employees)
    rows = [(f"E{index:04d}", "E0500", "manager") for index in range(500)]
    content = build_relationship_workbook_bytes(rows)

    with CaptureQueriesContext(connection) as queries:
        batch = preview_relationship_upload(content, "relations.xlsx", hr_admin)

    assert batch.valid_count == 500
    assert len(queries) <= 10
