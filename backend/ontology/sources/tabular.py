"""Bounded, domain-neutral table intake. Values are preserved as text or missing cells.

This module suggests field types, never business meaning, uniqueness or relationships.
It does not execute formulas, fetch links, publish models or write business objects.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import iterparse

MAX_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 200
MAX_CELLS = 200000
MAX_CELL_LENGTH = 100000
MAX_TABLES = 32


def _check_archive(archive):
    members = archive.infolist()
    if len(members) > 4096 or sum(item.file_size for item in members) > MAX_EXPANDED_BYTES:
        raise TabularError("工作簿展开后超出处理预算，请拆分文件")
    if len({item.filename for item in members}) != len(members):
        raise TabularError("工作簿包含重复内部文件，请重新导出")
    consumed = 0
    for item in members:
        if item.filename.lower().endswith("vbaproject.bin"):
            raise TabularError("不接收含宏的工作簿，请另存为不含宏的 XLSX")
        with archive.open(item) as stream:
            data = stream.read(MAX_EXPANDED_BYTES - consumed + 1)
        consumed += len(data)
        if consumed > MAX_EXPANDED_BYTES:
            raise TabularError("工作簿展开后超出处理预算，请拆分文件")
        if not item.filename.endswith((".xml", ".rels")):
            continue
        worksheet = item.filename.startswith("xl/worksheets/")
        for _, element in iterparse(io.BytesIO(data), events=("end",), forbid_dtd=True):
            if worksheet:
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "row" and int(element.get("r", "0")) > MAX_ROWS + 1:
                    raise TabularError("工作表实际行号超出导入预算，请移除多余空白区域")
                if tag == "c":
                    coordinate = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", element.get("r", ""))
                    if coordinate:
                        column = 0
                        for char in coordinate[1]:
                            column = column * 26 + ord(char) - 64
                            if column > MAX_COLUMNS:
                                raise TabularError("工作表实际单元格超出导入预算")
                        if column > MAX_COLUMNS or int(coordinate[2]) > MAX_ROWS + 1:
                            raise TabularError("工作表实际单元格超出导入预算")
            element.clear()


class TabularError(ValueError):
    """Safe public error without raw cell content."""


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str | None, ...], ...]
    source_rows: tuple[int, ...]
    blank_rows: int = 0
    formula_cells: int = 0


def _table(name, records):
    columns = None
    rows, source_rows = [], []
    blank_rows = formula_cells = 0
    for line, values, formulas in records:
        if not values or all(value is None or value == "" for value in values):
            blank_rows += 1
            continue
        if columns is None:
            if formulas:
                raise TabularError("表头不能使用公式，请改成明确的列名")
            columns = tuple(str(value).strip() if value is not None else "" for value in values)
            if len(columns) > MAX_COLUMNS:
                raise TabularError(f"每张表最多支持 {MAX_COLUMNS} 列")
            if any(not value or len(value) > 120 for value in columns):
                raise TabularError("表头必须非空且每列名称不超过 120 个字符")
            if len(set(columns)) != len(columns):
                raise TabularError("表头存在重复列名，请先区分同名列")
            continue
        if len(values) != len(columns):
            raise TabularError(f"第 {line} 行的列数与表头不同，请检查分隔符和缺失单元格")
        if any(value is not None and len(value) > MAX_CELL_LENGTH for value in values):
            raise TabularError(f"第 {line} 行包含超出长度限制的单元格")
        rows.append(tuple(None if value == "" else value for value in values))
        source_rows.append(line)
        formula_cells += formulas
        if len(rows) > MAX_ROWS or len(rows) * len(columns) > MAX_CELLS:
            raise TabularError(f"表格超过 {MAX_ROWS} 行或 {MAX_CELLS} 单元格的导入预算")
    if columns is None:
        raise TabularError("文件或工作表没有可用表头")
    if not rows:
        raise TabularError("表格只有表头，没有可导入的数据行")
    return Table(name, columns, tuple(rows), tuple(source_rows), blank_rows, formula_cells)


def read_tables(data: bytes, filename: str, *, encoding="utf-8-sig", delimiter=",") -> tuple[Table, ...]:
    if not data or len(data) > MAX_BYTES:
        raise TabularError("上传表格不能为空，且不能超过 10 MiB")
    suffix = filename.rsplit(".", 1)[-1].lower()
    if suffix == "csv":
        if encoding not in {"utf-8-sig", "gb18030", "utf-16"} or delimiter not in {",", ";", "\t"}:
            raise TabularError("请选择支持的字符编码和分隔符")
        try:
            text = data.decode(encoding)
            if "\x00" in text:
                raise TabularError("CSV 含有空字节，请检查编码或文件格式")
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            table = _table(filename, ((reader.line_num, row, 0) for row in reader))
            return (table,)
        except UnicodeError:
            raise TabularError("无法按所选编码读取 CSV，请选择正确编码后重试") from None
        except csv.Error:
            raise TabularError("CSV 引号或单元格长度不合法，未截断或跳过错误记录") from None
    if suffix != "xlsx":
        raise TabularError("目前支持 CSV 和 XLSX；请将旧版 Excel 或宏工作簿另存为 XLSX")
    try:
        with ZipFile(io.BytesIO(data)) as archive:
            _check_archive(archive)
        from openpyxl import load_workbook

        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            if len(book.worksheets) > MAX_TABLES:
                raise TabularError(f"一个工作簿最多支持 {MAX_TABLES} 张工作表")
            result = []
            for sheet in book.worksheets:
                # Declared dimensions may understate real data. Read actual cells;
                # archive preflight bounds sparse coordinates before this scan.
                sheet.reset_dimensions()
                sheet.calculate_dimension(force=True)
                if (sheet.max_column or 0) > MAX_COLUMNS or (sheet.max_row or 0) > MAX_ROWS + 1:
                    raise TabularError("工作表声明的行列范围超过导入预算，请移除多余空白区域")

                def records(sheet=sheet):
                    for row_number, cells in enumerate(sheet.iter_rows(), 1):
                        values, formulas = [], 0
                        for cell in cells:
                            value = cell.value
                            formulas += int(cell.data_type == "f")
                            if cell.data_type == "f" and not isinstance(value, str):
                                raise TabularError("数组或数据表公式须先转为明确的文本或值后导入")
                            if value is None:
                                values.append(None)
                            elif isinstance(value, bool):
                                values.append("true" if value else "false")
                            elif isinstance(value, (datetime, date)):
                                values.append(value.isoformat())
                            elif isinstance(value, int) and re.fullmatch(
                                r"0{2,80}", cell.number_format or ""
                            ):
                                values.append(
                                    ("-" if value < 0 else "")
                                    + str(abs(value)).zfill(len(cell.number_format))
                                )
                            else:
                                values.append(str(value))
                        yield row_number, values, formulas

                table = _table(sheet.title, records())
                result.append(table)
                if sum(len(item.rows) * len(item.columns) for item in result) > MAX_CELLS:
                    raise TabularError("工作簿总单元格数量超出导入预算，请拆分文件")
            return tuple(result)
        finally:
            book.close()
    except TabularError:
        raise
    except (
        BadZipFile,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ParseError,
        DefusedXmlException,
        RuntimeError,
    ):
        raise TabularError("无法读取该 XLSX 工作簿，请检查格式或重新导出") from None


def describe_table(table: Table):
    """Entire uploaded table, not a sampled guarantee for a live external source."""
    fields = []
    for index, label in enumerate(table.columns):
        values = [row[index] for row in table.rows if row[index] is not None]
        kind = "string"
        reason = "默认保留原始文本，业务含义由你确认"
        identifier = re.search(r"(?i)(?:^id$|[_\s]id$|code|phone|编号|编码|代码|电话|邮编|证件)", label)
        if values and not identifier:
            if all(value in {"true", "false"} for value in values):
                kind, reason = "boolean", "非空值均为 true 或 false"
            elif all(
                re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) and len(value.lstrip("-")) <= 1000
                for value in values
            ):
                kind, reason = "integer", "非空值均为无前导零的整数；原值仍保留"
            elif all(
                len(value) <= 1000 and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value)
                for value in values
            ):
                kind, reason = "number", "非空值均为十进制数；金额单位需另行确认"
        elif identifier:
            reason = "标识类字段保留为文本，不删除前导零或推断数值含义"
        fields.append(
            {
                "column": index,
                "label": label,
                "suggested_type": kind,
                "reason": reason,
                "missing_count": len(table.rows) - len(values),
                "distinct_count": len(set(values)),
            }
        )
    return {
        "name": table.name,
        "row_count": len(table.rows),
        "fields": fields,
        "blank_rows_skipped": table.blank_rows,
        "formula_cells_as_text": table.formula_cells,
        "scope": "entire_uploaded_table",
        "empty_cell_policy": "missing",
        "constraints_inferred": False,
        "relations_inferred": False,
    }
