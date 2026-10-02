"""Excel exports contain customer data, never executable spreadsheet formulas."""


def literal_strings(workbook) -> None:
    """Preserve exact text while overriding openpyxl's formula/error inference.

    Both exports are snapshots of computed server values. Even a string that
    starts with '=' is data here; numeric/date cells retain their native types.
    """
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = "s"
