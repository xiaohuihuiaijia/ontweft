import json

import pytest
from ontology.intelligence.prompts import PAGE_SCHEMA
from tests.support.intelligence import SyntheticModel, create_case, create_proposal, run_worker


class ReviewingModel(SyntheticModel):
    def __init__(self, monkeypatch):
        super().__init__(monkeypatch)
        self.page_inputs = []

    def generate(self, role, **kwargs):
        result = super().generate(role, **kwargs)
        if kwargs["schema"] == PAGE_SCHEMA:
            self.page_inputs.append((role, json.loads(kwargs["text"]), len(kwargs["images"])))
            result.data["needs_review"] = role == "extraction"
            result.data["issues"] = ["Synthetic dense table requires detail"] if role == "extraction" else []
        return result


@pytest.mark.parametrize("quality,expected_counts", [("balanced", [1, 3]), ("high", [1])])
def test_only_existing_upgrade_path_sends_pdf_details(platform, monkeypatch, quality, expected_counts):
    model = ReviewingModel(monkeypatch)
    case = create_case(platform, pages=1)
    ready = run_worker(case, create_proposal(case, quality=quality))
    assert ready["status"] == "ready", ready["job"]
    assert [entry[2] for entry in model.page_inputs] == expected_counts
    if quality == "balanced":
        role, context, _ = model.page_inputs[-1]
        assert role == "synthesis" and context["page"] == 1
        assert [region["region"] for region in context["image_regions"]] == ["overview", "top", "bottom"]
        assert context["image_regions"][0]["pixels"][1] <= 1281
        assert all(max(region["pixels"]) <= 3201 for region in context["image_regions"])
    assert ready["candidates"][0]["evidence"][0]["page"] == 1
    assert "bbox" not in ready["candidates"][0]["evidence"][0]
