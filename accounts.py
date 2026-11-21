"""Read account spreadsheets without evaluating cells or changing passwords."""
from contextlib import closing
import csv
from dataclasses import dataclass, field
import io
from pathlib import Path
import zipfile


@dataclass(frozen=True)
class Account:
    source_line: int
    email: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class AccountImport:
    accounts: list[Account] = field(repr=False)
    skipped_count: int = 0
    deferred_count: int = 0
    duplicate_count: int = 0


def spreadsheet_rows(data, suffix):
    """Yield row values and formula presence without retaining the whole sheet."""
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 200 * 1024 * 1024:
                    raise ValueError("Excel 解压后过大，请拆分表格。")
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            try:
                sheet = book.active
                if sheet.max_column and sheet.max_column > 30:
                    raise ValueError("表格最多 30 列。")
                for row in sheet.iter_rows():
                    yield [cell.value for cell in row], any(cell.data_type == "f" for cell in row)
            finally:
                book.close()
        except ValueError:
            raise
        except Exception:
            raise ValueError("无法读取 Excel，请使用有效的 .xlsx 文件。") from None
    elif suffix in (".csv", ".tsv", ".txt"):
        try:
            text = data.decode("utf-8-sig")
            first_line = text.partition("\n")[0]
            delimiter = "\t" if suffix != ".csv" or "\t" in first_line else ","
            for row in csv.reader(io.StringIO(text), delimiter=delimiter, strict=True):
                yield row, False
        except (UnicodeError, csv.Error):
            raise ValueError("无法读取文本表格，请使用 UTF-8 编码的 CSV 或 TSV。") from None
    else:
        raise ValueError("支持 .xlsx、.csv、.tsv 和 .txt 表格。")


def parse_import(data, filename, *, skip_success=True, max_accounts=None):
    suffix = Path(filename).suffix.lower()
    max_mb = 50 if suffix == ".xlsx" else 5
    if len(data) > max_mb * 1024 * 1024:
        raise ValueError(f"表格不能超过 {max_mb} MB。")
    if max_accounts is not None and (type(max_accounts) is not int or max_accounts < 1):
        raise ValueError("账号数量必须是正整数，留空表示不限。")
    accounts, seen = [], set()
    columns = None
    status_column = None
    skipped = deferred = duplicates = 0
    with closing(spreadsheet_rows(data, suffix)) as rows:
        for number, (row, has_formula) in enumerate(rows, 1):
            if not row or all(value in (None, "") for value in row):
                continue
            if isinstance(row[0], str) and row[0].lstrip().startswith("#"):
                continue
            if columns is None:
                header = [str(value or "").strip().casefold() for value in row]
                email_col = next((i for i, v in enumerate(header) if v in ("email", "邮箱", "账号", "account")), None)
                pass_col = next((i for i, v in enumerate(header) if v in ("password", "密码", "pass")), None)
                if email_col is not None and pass_col is not None:
                    if has_formula:
                        raise ValueError(f"第 {number} 行包含公式，请先粘贴为文本。")
                    columns = (email_col, pass_col)
                    status_column = next((i for i, v in enumerate(header) if v in ("status", "状态", "查询状态")), None)
                    continue
                columns = (0, 1)
            if skip_success and status_column is not None and status_column < len(row):
                status = row[status_column]
                if isinstance(status, str) and status.strip().casefold() in ("success", "已完成", "成功", "needs-human", "需人工验证", "需要人工验证"):
                    skipped += 1
                    continue
            email_value = row[columns[0]] if columns[0] < len(row) else None
            account_key = email_value.strip().casefold() if isinstance(email_value, str) else None
            if account_key and account_key in seen:
                duplicates += 1
                continue
            if max_accounts is not None and len(accounts) >= max_accounts:
                deferred += 1
                if account_key:
                    seen.add(account_key)
                continue
            if has_formula:
                raise ValueError(f"第 {number} 行包含公式，请先粘贴为文本。")
            if max(columns) >= len(row):
                raise ValueError(f"第 {number} 行缺少邮箱或密码列。")
            email, password = (row[i] for i in columns)
            if not isinstance(email, str) or not isinstance(password, str):
                raise ValueError(f"第 {number} 行的邮箱和密码必须是文本；Excel 请设置为文本格式。")
            email = email.strip()
            if email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@")) or not password:
                raise ValueError(f"第 {number} 行邮箱格式错误或密码为空。")
            if len(email) > 320 or len(password) > 1024:
                raise ValueError(f"第 {number} 行字段过长。")
            seen.add(email.casefold())
            accounts.append(Account(number, email, password))
    if not accounts and not skipped:
        raise ValueError("表格里没有账号。")
    return AccountImport(accounts, skipped_count=skipped, deferred_count=deferred, duplicate_count=duplicates)


def parse_accounts(data, filename, *, skip_success=True, max_accounts=None):
    return parse_import(data, filename, skip_success=skip_success, max_accounts=max_accounts).accounts
