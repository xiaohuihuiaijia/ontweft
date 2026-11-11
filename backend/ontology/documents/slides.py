"""Native PPTX text, table and embedded-image structure; no Office runtime."""

import hashlib
import posixpath

from ontology.documents.models import DocumentError, PageStatus, TableCell
from ontology.documents.parser import A, R, _xml, _zip

P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _relationships(archive, owner):
    folder, name = posixpath.split(owner)
    member = posixpath.join(folder, "_rels", name + ".rels")
    if member not in archive.namelist():
        return {}
    return {
        rel.get("Id"): posixpath.normpath(
            rel.get("Target", "").lstrip("/")
            if rel.get("Target", "").startswith("/")
            else posixpath.join(folder, rel.get("Target", ""))
        )
        for rel in _xml(archive.read(member))
        if rel.get("TargetMode") != "External"
    }


def parse_slides(data, builder):
    with _zip(data, builder.limits) as archive:
        root = _xml(archive.read("ppt/presentation.xml"))
        rels = _relationships(archive, "ppt/presentation.xml")
        slides = root.findall(P + "sldIdLst/" + P + "sldId")
        if len(slides) > builder.limits.max_pages:
            raise DocumentError("resource_limit", "Presentation exceeds the slide budget")
        images = 0
        for number, item in enumerate(slides, 1):
            member = rels.get(item.get(R + "id"))
            if not member or not member.startswith("ppt/") or member not in archive.namelist():
                raise DocumentError("invalid_document", "Slide relationship is missing")
            slide = _xml(archive.read(member))
            slide_rels = _relationships(archive, member)
            status = PageStatus(page_number=number, status="parsed")
            tree = slide.find(P + "cSld/" + P + "spTree")
            for index, shape in enumerate(tree.iter() if tree is not None else []):
                path = f"{member}/shape/{index}"
                if shape.tag == P + "sp":
                    text = "\n".join(
                        "".join(t.text or "" for t in paragraph.iter(A + "t"))
                        for paragraph in shape.findall(P + "txBody/" + A + "p")
                    )
                    if text:
                        placeholder = shape.find(P + "nvSpPr/" + P + "nvPr/" + P + "ph")
                        heading = placeholder is not None and placeholder.get("type") in {"title", "ctrTitle"}
                        builder.add(
                            "heading" if heading else "paragraph",
                            text,
                            path,
                            level=1 if heading else None,
                            evidence=builder.ref(path, page_number=number),
                        )
                elif shape.tag == A + "tbl":
                    cells = []
                    rows = shape.findall(A + "tr")
                    width = len(shape.findall(A + "tblGrid/" + A + "gridCol"))
                    for ri, row in enumerate(rows):
                        for ci, cell in enumerate(row.findall(A + "tc")):
                            if cell.get("hMerge") == "1" or cell.get("vMerge") == "1":
                                continue
                            text = "\n".join(
                                "".join(t.text or "" for t in p.iter(A + "t"))
                                for p in cell.findall(A + "txBody/" + A + "p")
                            )
                            cells.append(
                                TableCell(
                                    row=ri,
                                    column=ci,
                                    text=text,
                                    row_span=max(1, int(cell.get("rowSpan", "1"))),
                                    column_span=max(1, int(cell.get("gridSpan", "1"))),
                                    evidence=builder.ref(f"{path}/row/{ri}/cell/{ci}", page_number=number),
                                )
                            )
                    builder.add(
                        "table",
                        "\n".join(c.text for c in cells),
                        path,
                        cells=cells,
                        rows=len(rows),
                        columns=width,
                        evidence=builder.ref(path, page_number=number),
                    )
                elif shape.tag == P + "pic":
                    blip = shape.find(P + "blipFill/" + A + "blip")
                    target = slide_rels.get(blip.get(R + "embed")) if blip is not None else None
                    if not target or not target.startswith("ppt/") or target not in archive.namelist():
                        status.status, status.reason = "failed", "Embedded picture is unavailable"
                        builder.ir.complete = False
                        continue
                    images += 1
                    if images > builder.limits.max_images:
                        status.status, status.reason = "skipped", "Embedded image budget reached"
                        builder.ir.complete = False
                        continue
                    blob = archive.read(target)
                    builder.add(
                        "image",
                        "",
                        path,
                        evidence=builder.ref(path, page_number=number),
                        metadata={
                            "archive_member": target,
                            "content_hash": hashlib.sha256(blob).hexdigest(),
                            "bytes": len(blob),
                        },
                    )
                    status.status, status.reason = "needs_ocr", "Embedded image requires visual understanding"
                    builder.ir.complete = False
                elif shape.tag.endswith("}chart"):
                    target = slide_rels.get(shape.get(R + "id"))
                    if target and target.startswith("ppt/") and target in archive.namelist():
                        chart = _xml(archive.read(target))
                        text = "\n".join(
                            node.text for node in chart.iter() if node.tag.endswith("}v") and node.text
                        )
                        builder.add(
                            "paragraph",
                            text,
                            path,
                            evidence=builder.ref(path, page_number=number),
                            metadata={
                                "chart_source": target,
                                "review_reason": "Chart cache values require interpretation with slide context",
                            },
                        )
            builder.ir.pages.append(status)
        if not builder.ir.complete:
            builder.ir.quality = "needs_review"
        builder.ir.warnings.append("Slide numbers are native PPTX positions; slide geometry is not inferred")
