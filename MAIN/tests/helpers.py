"""Test helpers that use only generated fake data."""

from io import BytesIO, StringIO
import csv

from openpyxl import Workbook


ROSTER_HEADERS = (
    "员工编号",
    "姓名",
    "企业邮箱",
    "一级部门",
    "二级部门",
    "员工类别编码",
    "企业微信UserId",
)
RELATIONSHIP_HEADERS = ("被评价人工号", "评价人工号", "关系类型")


def build_xlsx_bytes(headers, rows) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def build_csv_bytes(headers, rows) -> bytes:
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def build_roster_workbook_bytes(rows) -> bytes:
    return build_xlsx_bytes(ROSTER_HEADERS, rows)


def build_relationship_workbook_bytes(rows) -> bytes:
    return build_xlsx_bytes(RELATIONSHIP_HEADERS, rows)
