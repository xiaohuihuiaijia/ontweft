from types import SimpleNamespace

import pytest
from ontology.core.errors import DomainError
from ontology.sources.ingestion import mapped_properties, transform_value


def test_integer_source_conversion_checks_size_before_exponent_expansion():
    for value in ("1e1000", "-1e1000", "1e1000000"):
        with pytest.raises(ValueError, match="1000"):
            transform_value(value, "integer")
    assert transform_value("9e999", "integer") == 9 * 10**999
    assert transform_value("0e1000000", "integer") == 0
    assert transform_value("9223372036854775807", "integer") == 9223372036854775807


def test_excessive_integer_becomes_a_mapping_error_with_record_evidence():
    record = SimpleNamespace(id="selected-source-record", values={"raw": "1e1000000"})
    mapping = {"fields": {"count": {"source_field": "raw", "transform": "integer"}}}
    definition = {"properties": [{"key": "count", "data_type": "integer"}]}
    with pytest.raises(DomainError) as raised:
        mapped_properties(record, mapping, definition)
    assert raised.value.code == "mapping_conversion_failed"
    assert raised.value.details == {"record_id": record.id, "field": "count"}
