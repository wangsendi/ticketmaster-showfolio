"""Size-limited Excel exports, result tables and original screenshot bundles."""
import csv
from collections import OrderedDict
import hashlib
import io
import math
import re
from pathlib import Path
import tempfile
import zipfile


SCREENSHOT_KINDS = {"full-page": "整页长截图", "legacy-viewport": "历史第一屏截图", "failure": "失败时页面"}
ANSI_CODES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
INVALID_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
EXCEL_MAX_BYTES = 100_000_000


class ExportSizeError(ValueError):
    pass


def excel_text(value, *, credential=False):
    # Browser errors contain ANSI color codes. Keep logs readable, while
    # representing other XML-forbidden characters as visible Unicode escapes.
    # Tabs/newlines/carriage returns and ordinary credentials stay unchanged.
    if not credential:
        value = ANSI_CODES.sub("", value)
    return INVALID_XML.sub(lambda match: f"\\u{ord(match.group()):04x}", value)


def workbook_screenshot(raw, max_bytes=None):
    """Compress only the exported copy, retaining the entire page."""
    from PIL import Image

    # Tiny PNGs already compress better than a JPEG header.
    if len(raw) < 4096 and (max_bytes is None or len(raw) <= max_bytes):
        return raw, ".png"
    with Image.open(io.BytesIO(raw)) as source:
        if source.height > 65000 and max_bytes is None:
            return raw, ".png"
        # A white background preserves screenshots with alpha transparency.
        if source.mode in ("RGBA", "LA") or "transparency" in source.info:
            rgba = source.convert("RGBA")
            image = Image.new("RGB", source.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
            rgba.close()
        else:
            image = source.convert("RGB")
        try:
            scale = min(1, 1440 / image.width, 65000 / image.height)
            if scale < 1:
                resized = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
                image.close()
                image = resized
            quality = 75
            for attempt in range(12):
                encoded = io.BytesIO()
                # Preserve color detail around text whenever the budget allows.
                image.save(encoded, format="JPEG", quality=quality, subsampling=0)
                data = encoded.getvalue()
                if max_bytes is None or min(len(data), len(raw)) <= max_bytes:
                    return (data, ".jpeg") if len(data) < len(raw) else (raw, ".png")
                scale = max(0.25, min(0.85, math.sqrt(max_bytes / len(data)) * 0.9))
                resized = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.Resampling.LANCZOS)
                image.close()
                image = resized
                quality = max(30, quality - 5)
            raise ExportSizeError("截图无法压缩到导出大小预算内。")
        finally:
            image.close()


def results_csv(rows, screenshots=None):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    columns = ["邮箱", "密码", "状态", "所选场次", "失败原因", "查询时间", "Cookie"]
    if screenshots is not None:
        columns.extend(["截图文件", "截图类型"])
    writer.writerow(columns)
    for row in rows:
        selected = row["selections"]
        fields = [row["email"], row["password"], row["status"], row.get("verification_requirement") or ("；".join(v["name"] for v in selected) if selected is not None else "未取得场次"),
                  row["error"] or "", row["finished_at"] or "", row["cookie_header"] or ""]
        if screenshots is not None:
            fields.extend(screenshots.get(row["id"], ("", "无截图")))
        # Credentials must round-trip exactly, including leading spaces and '='.
        # Excel exports use explicitly typed text cells for every column.
        writer.writerow(["'" + v if i not in (1, 6) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v
                         for i, v in enumerate(fields)])
    return stream.getvalue().encode("utf-8-sig")


def result_bundle(store, batch_id):
    rows = store.runs(batch_id, include_credentials=True)
    archive_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    screenshots = {}
    try:
        with zipfile.ZipFile(archive_file, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with store.connect() as db:
                for row in rows:
                    if not row["has_screenshot"]:
                        continue
                    image = db.execute("SELECT screenshot,screenshot_kind FROM runs WHERE id=?", (row["id"],)).fetchone()
                    if image["screenshot"] is None:
                        continue
                    filename = f"screenshots/account-{row['id']:06d}.png"
                    # PNG is already compressed. Preserve its original bytes and dimensions.
                    archive.writestr(filename, image["screenshot"], compress_type=zipfile.ZIP_STORED)
                    screenshots[row["id"]] = (filename, SCREENSHOT_KINDS.get(image["screenshot_kind"], "页面截图"))
            archive.writestr("results.csv", results_csv(rows, screenshots))
        archive_file.seek(0)
        return archive_file
    except BaseException:
        archive_file.close()
        raise


class WorkbookArchive(zipfile.ZipFile):
    def writestr(self, zinfo_or_arcname, data, compress_type=None, compresslevel=None):
        name = zinfo_or_arcname.filename if isinstance(zinfo_or_arcname, zipfile.ZipInfo) else zinfo_or_arcname
        if name.startswith("xl/media/"):
            compress_type = zipfile.ZIP_STORED
        return super().writestr(zinfo_or_arcname, data, compress_type=compress_type, compresslevel=compresslevel)


def results_workbook(store, batch_id, *, rows=None, destination=None, on_progress=None, max_bytes=EXCEL_MAX_BYTES):
    """Return one XLSX strictly below the size limit, without trimming accounts."""
    rows = store.runs(batch_id, include_credentials=True) if rows is None else rows
    image_count = sum(bool(row["has_screenshot"]) for row in rows)
    budget = max(1024, int(max_bytes * 0.85 / max(1, image_count)))
    for attempt in range(3):
        output = _results_workbook(store, batch_id, rows=rows, destination=destination,
                                   on_progress=on_progress, image_budget=budget)
        size = output.seek(0, 2)
        if size < max_bytes:
            output.seek(0)
            return output
        try:
            with zipfile.ZipFile(output) as archive:
                media_size = sum(item.compress_size for item in archive.infolist() if item.filename.startswith("xl/media/"))
            available = max_bytes - (size - media_size) - int(max_bytes * 0.01)
        finally:
            output.close()
        if not image_count or available < image_count * 1024:
            break
        budget = min(int(budget * 0.7), int(available * 0.9 / image_count))
    raise ExportSizeError("账号文字资料过大，无法在完整保留全部账号资料的同时将 Excel 控制在大小上限内。")


def _results_workbook(store, batch_id, *, rows, destination=None, on_progress=None, image_budget=None):
    """Embed compact full-page screenshots inside each account's row."""
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, OneCellAnchor
    from openpyxl.drawing.xdr import XDRPositiveSize2D
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.units import pixels_to_EMU
    from openpyxl.writer.excel import ExcelWriter

    rows = store.runs(batch_id, include_credentials=True) if rows is None else rows
    output = open(destination, "w+b") if destination else tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    book = Workbook()
    sheet = book.active
    sheet.title = "查询结果"
    sheet.sheet_view.showGridLines = False
    sheet.freeze_panes = "C2"
    columns = [("邮箱", 32), ("密码", 26), ("状态", 16), ("所选场次", 43), ("页面截图", 50),
               ("截图类型", 22), ("失败原因", 40), ("查询时间", 26), ("Cookie", 70)]
    # An Excel text cell holds at most 32,767 characters. Preserve longer
    # Cookie headers in adjacent, visible continuation columns.
    cookie_parts = max(1, max(((len(excel_text(row["cookie_header"] or "", credential=True)) + 32766) // 32767 for row in rows), default=1))
    columns.extend((f"Cookie（续 {index}）", 70) for index in range(2, cookie_parts + 1))
    states = {"pending": "等待查询", "running": "查询中", "success": "已完成", "partial": "结果不完整",
              "failed": "查询失败", "needs-human": "需人工验证", "cancelled": "已停止", "interrupted": "已中断"}
    edge = Side(style="hair", color="DFE5E3")
    text_font = Font(name="微软雅黑", size=11, color="202A2C")
    text_alignment = Alignment(vertical="top", wrap_text=True)
    text_border = Border(bottom=edge)
    fills = [PatternFill("solid", fgColor=color) for color in ("F5F8F6", "FFFFFF")]
    for index, (title, width) in enumerate(columns, 1):
        cell = sheet.cell(1, index, title)
        cell.font = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="186553")
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[cell.column_letter].width = width
    sheet.row_dimensions[1].height = 28
    try:
        # openpyxl loads each image again while saving. Private temporary files
        # keep a large batch from holding every PNG in memory at the same time.
        with tempfile.TemporaryDirectory(prefix="tm-excel-") as temporary:
            # Repeated failure pages are often byte-identical. Cache at most
            # 16 small encoded images (4 MiB), never the source PNG collection.
            image_cache = OrderedDict()
            with store.connect() as db:
                for row_number, row in enumerate(rows, 2):
                    selected = row["selections"]
                    events = row.get("verification_requirement") or ("未取得场次" if selected is None else "\n".join(v["name"] for v in selected) or "未选择场次")
                    cookie = excel_text(row["cookie_header"] or "", credential=True)
                    values = [row["email"], row["password"], states.get(row["status"], row["status"]), events, "暂无截图", "无截图",
                              row["error"] or "", row["finished_at"] or ""]
                    values.extend(cookie[index * 32767:(index + 1) * 32767] for index in range(cookie_parts))
                    for column, value in enumerate(values, 1):
                        cell = sheet.cell(row_number, column, excel_text(value, credential=column == 2 or column >= 9))
                        # Account data is text, including values beginning with '='.
                        cell.data_type = "s"
                        cell.font = text_font
                        cell.alignment = text_alignment
                        cell.fill = fills[row_number % 2]
                        cell.border = text_border
                    sheet.row_dimensions[row_number].height = 70
                    if on_progress:
                        on_progress(row_number - 1)
                    if not row["has_screenshot"]:
                        continue
                    record = db.execute("SELECT screenshot,screenshot_kind FROM runs WHERE id=?", (row["id"],)).fetchone()
                    if record["screenshot"] is None:
                        continue
                    image_key = hashlib.sha256(record["screenshot"]).digest()
                    encoded = image_cache.get(image_key)
                    if encoded is None:
                        encoded = workbook_screenshot(record["screenshot"], image_budget)
                        if len(encoded[0]) <= 256 * 1024:
                            image_cache[image_key] = encoded
                            if len(image_cache) > 16:
                                image_cache.popitem(last=False)
                    else:
                        image_cache.move_to_end(image_key)
                    image_data, extension = encoded
                    path = Path(temporary) / f"account-{row['id']}{extension}"
                    path.write_bytes(image_data)
                    image = Image(str(path))
                    scale = min(1, 330 / image.width, 500 / image.height)
                    width, height = image.width * scale, image.height * scale
                    # Stay within Excel's 409-point row-height limit, while the
                    # embedded media retains the entire page.
                    sheet.row_dimensions[row_number].height = max(70, math.ceil((height + 16) * 0.75))
                    image.anchor = OneCellAnchor(
                        _from=AnchorMarker(col=4, row=row_number - 1,
                                           colOff=pixels_to_EMU(8 + (330 - width) / 2), rowOff=pixels_to_EMU(8)),
                        ext=XDRPositiveSize2D(pixels_to_EMU(width), pixels_to_EMU(height)),
                    )
                    sheet.add_image(image)
                    sheet.cell(row_number, 5, "").data_type = "s"
                    sheet.cell(row_number, 6, SCREENSHOT_KINDS.get(record["screenshot_kind"], "页面截图"))
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{len(rows) + 1}"
            sheet.print_title_rows = "1:1"
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.page_setup.orientation = "landscape"
            sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
            sheet.page_setup.fitToWidth = 1
            sheet.page_setup.fitToHeight = 0
            with WorkbookArchive(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                ExcelWriter(book, archive).save()
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
    finally:
        book.close()
