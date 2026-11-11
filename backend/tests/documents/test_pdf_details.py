import base64
import io

import pytest
from ontology.documents.pages import iter_page_inputs
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


@pytest.mark.parametrize("width,height,names", [(400, 600, ["top", "bottom"]), (600, 400, ["left", "right"])])
def test_pdf_detail_images_cover_same_page_in_order_with_overlap(tmp_path, width, height, names):
    writer = PdfWriter()
    page = writer.add_blank_page(width=width, height=height)
    stream = DecodedStreamObject()
    if height >= width:
        stream.set_data(
            f"1 0 0 rg 0 {height / 2} {width} {height / 2} re f\n0 0 1 rg 0 0 {width} {height / 2} re f".encode()
        )
    else:
        stream.set_data(
            f"1 0 0 rg 0 0 {width / 2} {height} re f\n0 0 1 rg {width / 2} 0 {width / 2} {height} re f".encode()
        )
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "synthetic-regions.pdf"
    with path.open("wb") as output:
        writer.write(output)
    inputs = iter_page_inputs(
        path,
        path.name,
        source_id="synthetic",
        source_revision="1",
        page_numbers=[1],
        image_max_edge=512,
        detail_images=True,
    )
    try:
        result = next(inputs)
    finally:
        inputs.close()
    assert result["page_number"] == 1 and result["evidence"]["page_number"] == 1
    assert result["evidence"].get("bbox") is None
    assert result["image_count"] == 3
    regions = result["image_regions"]
    assert [region["image_index"] for region in regions] == [0, 1, 2]
    assert [region["region"] for region in regions] == ["overview", *names]
    assert regions[0]["rect"] == [0, 0, 1, 1]
    first, second = regions[1]["rect"], regions[2]["rect"]
    axis = 1 if height >= width else 0
    assert first[axis] == 0 and second[axis + 2] == 1
    assert first[axis + 2] - second[axis] == pytest.approx(0.08)
    images = [
        Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))) for uri in result["image_data_urls"]
    ]
    try:
        assert all(max(image.size) <= 513 for image in images)
        assert images[1].getpixel((images[1].width // 2, images[1].height // 2))[:3] == (255, 0, 0)
        assert images[2].getpixel((images[2].width // 2, images[2].height // 2))[:3] == (0, 0, 255)
        # Detail crops carry more pixels per page unit than the context image.
        assert images[1].width / ((first[2] - first[0]) * width) > images[0].width / width
    finally:
        for image in images:
            image.close()


def test_normal_pdf_input_keeps_one_image(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    path = tmp_path / "synthetic.pdf"
    with path.open("wb") as output:
        writer.write(output)
    pages = list(
        iter_page_inputs(path, path.name, source_id="synthetic", source_revision="1", image_max_edge=512)
    )
    assert pages[0]["image_count"] == 1 and len(pages[0]["image_data_urls"]) == 1
