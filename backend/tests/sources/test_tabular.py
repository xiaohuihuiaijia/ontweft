import io
from datetime import date

import pytest
from ontology.sources.tabular import TabularError, describe_table, read_tables
from openpyxl import Workbook


def test_csv_keeps_identifiers_exact_numbers_and_original_row_coordinates():
    (table,) = read_tables(
        '编号,名称,数量,金额\r\n001,"多行\n名称",9223372036854775809,1.20\r\n002,其他,,0\r\n'.encode(
            "utf-8-sig"
        ),
        "库存.csv",
    )
    assert table.rows[0] == ("001", "多行\n名称", "9223372036854775809", "1.20")
    assert table.rows[1][2] is None
    assert table.source_rows == (3, 4)
    profile = describe_table(table)
    assert [field["suggested_type"] for field in profile["fields"]] == [
        "string",
        "string",
        "integer",
        "number",
    ]
    assert profile["fields"][2]["missing_count"] == 1
    assert profile["constraints_inferred"] is False and profile["relations_inferred"] is False


def test_csv_encoding_and_separator_are_explicit_and_blank_rows_are_reported():
    (table,) = read_tables(
        "代码;名称\n\n0001;阀门\n".encode("gb18030"), "资产.csv", encoding="gb18030", delimiter=";"
    )
    assert table.blank_rows == 1 and table.rows == (("0001", "阀门"),)
    with pytest.raises(TabularError, match="编码"):
        read_tables("代码;名称\n0001;阀门".encode("gb18030"), "资产.csv")


@pytest.mark.parametrize(
    "content,message",
    [
        ("名称,名称\na,b", "重复"),
        ("名称,\na,b", "非空"),
        ("名称,数量\na,1,extra", "列数"),
        ("名称\n", "没有可导入"),
        ('名称\n"unclosed', "引号"),
    ],
)
def test_csv_rejects_ambiguous_or_malformed_tables(content, message):
    with pytest.raises(TabularError, match=message):
        read_tables(content.encode(), "data.csv")


def test_xlsx_multiple_sheets_preserve_display_codes_dates_and_formula_text():
    book = Workbook()
    sheet = book.active
    sheet.title = "设备"
    sheet.append(["编号", "日期", "计算值"])
    sheet.append([7, date(2026, 11, 10), "=1+1"])
    sheet["A2"].number_format = "000000"
    other = book.create_sheet("位置")
    other.append(["名称", "启用"])
    other.append(["一层", True])
    output = io.BytesIO()
    book.save(output)
    book.close()
    asset, location = read_tables(output.getvalue(), "资料.xlsx")
    assert asset.rows[0] == ("000007", "2026-11-10T00:00:00", "=1+1")
    assert describe_table(asset)["formula_cells_as_text"] == 1
    assert location.rows == (("一层", "true"),)


def test_budget_and_format_errors_do_not_partially_return_data(monkeypatch):
    monkeypatch.setattr("ontology.sources.tabular.MAX_ROWS", 1)
    with pytest.raises(TabularError, match="预算"):
        read_tables(b"name\none\ntwo\n", "data.csv")
    with pytest.raises(TabularError, match="XLSX"):
        read_tables(b"not a workbook", "data.xlsx")
    with pytest.raises(TabularError, match="CSV"):
        read_tables(b"not a workbook", "data.xlsm")
