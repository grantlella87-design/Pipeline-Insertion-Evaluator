"""Tests for reading coded-value domains out of layer metadata.

The pressure-units check is the important one. Every main's bucket depends on
reading `pressureunits` correctly, so a service whose unit domain does not
match the codes this project assumes would put the whole network in the wrong
bucket - and the run would otherwise complete without comment.
"""
from pipelineinsertion import config, domains

# A layer that keeps ASSETTYPE under per-subtype domains, the way UPDM does,
# and pressureunits as a plain coded-value domain on the field.
LAYER_JSON = {
    "id": 145,
    "name": "Main Lines",
    "typeIdField": "ASSETGROUP",
    "fields": [
        {"name": "ASSETTYPE"},
        {
            "name": "pressureunits",
            "domain": {
                "name": "7_UPDM_UnitsForPressure",
                "codedValues": [
                    {"code": 0, "name": "Unknown"},
                    {"code": 1, "name": "Pounds/Square Inch"},
                    {"code": 2, "name": "Inch Water Column"},
                ],
            },
        },
    ],
    "types": [
        {
            "id": 1,
            "name": "Distribution Main",
            "domains": {
                "ASSETTYPE": {
                    "name": "MainAssetType",
                    "codedValues": [
                        {"code": 1, "name": "Bare Steel"},
                        {"code": 2, "name": "Cast Iron"},
                        {"code": 3, "name": "Coated Steel"},
                    ],
                }
            },
        },
        {
            "id": 2,
            "name": "Transmission Main",
            "domains": {
                "ASSETTYPE": {
                    "name": "MainAssetType",
                    "codedValues": [
                        {"code": 3, "name": "Coated Steel"},
                        {"code": 5, "name": "Copper"},
                        {"code": 12, "name": "Wrought Iron"},
                    ],
                }
            },
        },
    ],
}


class TestFieldDomainLabels:
    def test_reads_a_plain_coded_value_domain(self):
        labels = domains.field_domain_labels(LAYER_JSON, "pressureunits")
        assert labels == {0: "Unknown", 1: "Pounds/Square Inch",
                          2: "Inch Water Column"}

    def test_field_name_match_ignores_case(self):
        assert domains.field_domain_labels(LAYER_JSON, "PRESSUREUNITS")

    def test_a_field_with_no_domain_is_empty(self):
        assert domains.field_domain_labels(LAYER_JSON, "ASSETTYPE") == {}


class TestSubtypeDomainLabels:
    def test_flattens_across_subtypes(self):
        labels = domains.subtype_domain_labels(LAYER_JSON, "ASSETTYPE")
        assert labels[1] == "Bare Steel"
        assert labels[5] == "Copper"
        assert labels[12] == "Wrought Iron"

    def test_a_code_meaning_the_same_thing_in_two_subtypes_is_kept(self):
        # Code 3 is Coated Steel in both subtypes, so there is no conflict.
        assert domains.subtype_domain_labels(LAYER_JSON, "ASSETTYPE")[3] == (
            "Coated Steel")

    def test_a_conflicting_code_is_dropped_rather_than_guessed(self):
        """Two subtypes disagreeing about a code.

        Flattening picks whichever was read last, which would label some rows
        with a material they are not. The code is left unlabelled instead, so
        it reports as its number.
        """
        conflicted = {
            "types": [
                {"id": 1, "domains": {"ASSETTYPE": {"codedValues": [
                    {"code": 7, "name": "Plastic"}]}}},
                {"id": 2, "domains": {"ASSETTYPE": {"codedValues": [
                    {"code": 7, "name": "Ductile Iron"}]}}},
            ]
        }
        assert 7 not in domains.subtype_domain_labels(conflicted, "ASSETTYPE")


class TestLabelsFor:
    def test_prefers_the_plain_field_domain(self):
        assert domains.labels_for(LAYER_JSON, "pressureunits")[1] == (
            "Pounds/Square Inch")

    def test_falls_back_to_the_subtype_domains(self):
        assert domains.labels_for(LAYER_JSON, "ASSETTYPE")[2] == "Cast Iron"

    def test_no_metadata_is_empty(self):
        assert domains.labels_for(None, "ASSETTYPE") == {}
        assert domains.labels_for({}, "ASSETTYPE") == {}


class TestMaterialLabels:
    def test_covers_every_code_this_project_acts_on(self):
        labels = domains.material_labels(LAYER_JSON)
        for code in (config.ASSETTYPE_BARE_STEEL, config.ASSETTYPE_CAST_IRON,
                     config.ASSETTYPE_COATED_STEEL, config.ASSETTYPE_COPPER,
                     config.ASSETTYPE_WROUGHT_IRON):
            assert code in labels


class TestCheckPressureUnits:
    def test_a_matching_domain_passes(self):
        assert domains.check_pressure_units(LAYER_JSON) is True

    def test_no_metadata_is_not_a_failure(self):
        # Nothing to check against is not the same as a mismatch.
        assert domains.check_pressure_units({}) is True

    def test_a_swapped_domain_is_caught(self):
        """The single most consequential assumption in the workflow.

        If code 1 meant water column rather than PSI, every main would be
        bucketed on the wrong unit and the run would still finish.
        """
        swapped = {
            "fields": [{"name": "pressureunits", "domain": {"codedValues": [
                {"code": 1, "name": "Inch Water Column"},
                {"code": 2, "name": "Pounds/Square Inch"},
            ]}}]
        }
        assert domains.check_pressure_units(swapped) is False

    def test_a_missing_code_is_caught(self):
        incomplete = {
            "fields": [{"name": "pressureunits", "domain": {"codedValues": [
                {"code": 1, "name": "Pounds/Square Inch"},
            ]}}]
        }
        assert domains.check_pressure_units(incomplete) is False
