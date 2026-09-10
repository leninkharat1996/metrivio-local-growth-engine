import copy
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "final_icp"),
)

import qualify_final as qf  # noqa: E402

RULES_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "final_icp_rules.json")
RULES = qf.load_rules(RULES_PATH)
FIXED_EVALUATED_AT = "2026-09-09T00:00:00+00:00"


def make_prelim(**overrides):
    base = {
        "master_id": "abc123",
        "business_name": "Acme Commercial HVAC",
        "website": "https://acmehvac.com",
        "phone": "214-555-0100",
        "address": "123 Main St, Dallas, TX 75201",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "rating": 4.7,
        "review_count": 85,
        "icp_tier": "A",
        "icp_score": 80,
        "icp_status": "qualified",
        "icp_confidence": "high",
        "icp_evidence": ["commercial_hvac_signal"],
        "icp_reasons": ["base score: 30"],
        "icp_exclusions": [],
        "icp_evaluated_at": FIXED_EVALUATED_AT,
    }
    base.update(overrides)
    return base


def make_evidence(category, keyword=None, evidence_type="direct_service", page_url="https://acmehvac.com/services"):
    return {
        "category": category,
        "keyword": keyword or category.replace("_", " "),
        "page_url": page_url,
        "snippet": f"we provide {keyword or category}",
        "evidence_type": evidence_type,
    }


def make_enrichment(evidence=None, website_status="success", **overrides):
    base = {
        "master_id": "abc123",
        "business_name": "Acme Commercial HVAC",
        "website": "https://acmehvac.com",
        "website_status": website_status,
        "http_status": 200,
        "final_url": "https://acmehvac.com",
        "homepage_title": "Acme Commercial HVAC",
        "homepage_description": "Commercial HVAC services",
        "commercial_hvac_signals": 0,
        "commercial_service_signals": 0,
        "commercial_vertical_signals": 0,
        "residential_signals": 0,
        "evidence": evidence or [],
        "crawl_errors": [],
    }
    base.update(overrides)
    return base


def classify(prelim_overrides=None, evidence=None, website_status="success", enrichment_overrides=None, no_enrichment=False):
    prelim = make_prelim(**(prelim_overrides or {}))
    enrichment = None if no_enrichment else make_enrichment(evidence=evidence, website_status=website_status, **(enrichment_overrides or {}))
    return qf.qualify_final_record(prelim, enrichment, RULES, FIXED_EVALUATED_AT)


class QualificationPathTests(unittest.TestCase):
    def test_commercial_hvac_plus_chiller_qualifies(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")])
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertEqual(r["final_icp_confidence"], "high")

    def test_commercial_hvac_plus_maintenance_contract_qualifies(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("maintenance_contract")])
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_industrial_hvac_plus_preventive_maintenance_qualifies(self):
        r = classify(evidence=[make_evidence("industrial_hvac"), make_evidence("preventive_maintenance")])
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_commercial_mechanical_plus_mechanical_services_qualifies(self):
        r = classify(evidence=[make_evidence("commercial_mechanical"), make_evidence("mechanical_services")])
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_commercial_hvac_alone_with_high_prelim_qualifies(self):
        r = classify(evidence=[make_evidence("commercial_hvac")], prelim_overrides={"icp_confidence": "high"})
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertEqual(r["final_icp_confidence"], "high")

    def test_commercial_hvac_alone_with_medium_prelim_is_review(self):
        r = classify(evidence=[make_evidence("commercial_hvac")], prelim_overrides={"icp_confidence": "medium"})
        self.assertEqual(r["final_icp_status"], "review")
        self.assertEqual(r["final_icp_confidence"], "medium")

    def test_two_bucket2_only_no_hvac_is_review(self):
        r = classify(evidence=[make_evidence("preventive_maintenance"), make_evidence("service_contract")])
        self.assertEqual(r["final_icp_status"], "review")
        self.assertEqual(r["hvac_direct_signals"], [])

    def test_facility_services_design_build_no_hvac_is_review(self):
        r = classify(evidence=[make_evidence("facility_services"), make_evidence("design_build")])
        self.assertEqual(r["final_icp_status"], "review")

    def test_maintenance_contract_mechanical_services_no_hvac_is_review(self):
        r = classify(evidence=[make_evidence("maintenance_contract"), make_evidence("mechanical_services")])
        self.assertEqual(r["final_icp_status"], "review")

    def test_bucket2_only_with_high_prelim_confidence_is_still_review(self):
        r = classify(
            evidence=[make_evidence("service_contract")],
            prelim_overrides={"icp_confidence": "high"},
        )
        self.assertEqual(r["final_icp_status"], "review")

    def test_zero_hvac_evidence_with_high_score_still_review(self):
        evidence = [
            make_evidence("preventive_maintenance"),
            make_evidence("service_contract"),
            make_evidence("commercial_vertical", "office", evidence_type="customer_vertical"),
            make_evidence("commercial_vertical", "warehouse", evidence_type="customer_vertical"),
            make_evidence("commercial_vertical", "healthcare", evidence_type="customer_vertical"),
        ]
        r = classify(
            evidence=evidence,
            prelim_overrides={"rating": 4.9, "review_count": 500, "icp_confidence": "high"},
        )
        self.assertEqual(r["final_icp_status"], "review")
        self.assertNotIn(r["final_icp_tier"], ("A", "A+", "B"))

    def test_incidental_hvac_with_bucket2_categories_is_review(self):
        evidence = [
            make_evidence("commercial_hvac", evidence_type="incidental"),
            make_evidence("preventive_maintenance"),
            make_evidence("service_contract"),
        ]
        r = classify(evidence=evidence)
        self.assertEqual(r["final_icp_status"], "review")
        self.assertEqual(r["hvac_direct_signals"], [])


class ResidentialTests(unittest.TestCase):
    def test_residential_plus_commercial_hvac_qualifies(self):
        evidence = [
            make_evidence("commercial_hvac"),
            make_evidence("maintenance_contract"),
            make_evidence("residential", "residential", evidence_type="residential"),
        ]
        r = classify(evidence=evidence)
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertTrue(r["residential_signals"]["present"])

    def test_residential_only_is_excluded(self):
        evidence = [make_evidence("residential", "residential", evidence_type="residential")]
        r = classify(
            evidence=evidence,
            prelim_overrides={
                "icp_evidence": ["residential_only_signal"],
                "icp_exclusions": ["hard exclusion: residential-only keyword(s) ['residential'] matched with no commercial signal present"],
                "icp_confidence": "low",
                "icp_status": "excluded",
            },
        )
        self.assertEqual(r["final_icp_status"], "excluded")
        self.assertEqual(r["final_icp_tier"], "D")

    def test_A_preliminary_residential_only_rescued_by_website_direct_hvac_qualifies(self):
        # Test A (FIX 1 regression): a preliminary residential-only
        # exclusion must be rescuable by genuine website direct-service
        # HVAC evidence, exactly like the single-trade rescue.
        evidence = [
            make_evidence("commercial_hvac"),
            make_evidence("maintenance_contract"),
        ]
        r = classify(
            evidence=evidence,
            prelim_overrides={
                "icp_evidence": ["residential_only_signal"],
                "icp_exclusions": [
                    "hard exclusion: residential-only keyword(s) ['residential'] matched with no commercial signal present"
                ],
                "icp_confidence": "low",
                "icp_status": "excluded",
            },
        )
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertNotEqual(r["final_icp_status"], "excluded")
        self.assertEqual(r["exclusion_reasons"], [])

    def test_B_preliminary_residential_only_with_only_residential_website_evidence_stays_excluded(self):
        # Test B: no genuine commercial HVAC evidence on the website at
        # all -- the residential-only exclusion must NOT be rescued.
        evidence = [make_evidence("residential", "residential", evidence_type="residential")]
        r = classify(
            evidence=evidence,
            prelim_overrides={
                "icp_evidence": ["residential_only_signal"],
                "icp_exclusions": [
                    "hard exclusion: residential-only keyword(s) ['residential'] matched with no commercial signal present"
                ],
                "icp_confidence": "low",
                "icp_status": "excluded",
            },
        )
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_C_preliminary_residential_only_with_only_incidental_hvac_never_qualifies(self):
        # Test C: only INCIDENTAL commercial HVAC mention (not
        # direct_service) -- must never be rescued into qualified. It may
        # remain excluded (no direct rescue evidence) or fall to review,
        # but qualified is explicitly disallowed.
        evidence = [
            make_evidence("commercial_hvac", evidence_type="incidental"),
            make_evidence("residential", "residential", evidence_type="residential"),
        ]
        r = classify(
            evidence=evidence,
            prelim_overrides={
                "icp_evidence": ["residential_only_signal"],
                "icp_exclusions": [
                    "hard exclusion: residential-only keyword(s) ['residential'] matched with no commercial signal present"
                ],
                "icp_confidence": "low",
                "icp_status": "excluded",
            },
        )
        self.assertNotEqual(r["final_icp_status"], "qualified")
        self.assertIn(r["final_icp_status"], ("excluded", "review"))


class HardExclusionTests(unittest.TestCase):
    def test_hvac_supply_house_excluded_via_website_pattern(self):
        r = classify(
            evidence=[make_evidence("commercial_hvac", evidence_type="incidental"), make_evidence("chiller", evidence_type="incidental")],
            enrichment_overrides={"homepage_title": "Northline Supply | HVAC Parts and Equipment", "homepage_description": "HVAC parts and supplies counter."},
        )
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_manufacturer_excluded_via_preliminary_carrythrough(self):
        r = classify(
            evidence=[],
            prelim_overrides={
                "icp_exclusions": ["hard exclusion: business type keyword 'manufacturer' matched"],
                "icp_status": "excluded",
            },
        )
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_distributor_excluded_via_preliminary_carrythrough(self):
        r = classify(
            evidence=[],
            prelim_overrides={
                "icp_exclusions": ["hard exclusion: business type keyword 'distributor' matched"],
                "icp_status": "excluded",
            },
        )
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_plumbing_with_clear_commercial_hvac_is_rescued_and_qualifies(self):
        r = classify(
            evidence=[make_evidence("commercial_hvac"), make_evidence("maintenance_contract")],
            prelim_overrides={
                "icp_exclusions": [
                    "hard exclusion: plumbing_only keyword(s) ['plumbing'] matched with no HVAC signal present"
                ],
            },
        )
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_plumbing_only_without_website_rescue_stays_excluded(self):
        r = classify(
            evidence=[],
            prelim_overrides={
                "icp_exclusions": [
                    "hard exclusion: plumbing_only keyword(s) ['plumbing'] matched with no HVAC signal present"
                ],
            },
        )
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_non_us_is_excluded(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")], prelim_overrides={"country": "Canada"})
        self.assertEqual(r["final_icp_status"], "excluded")

    def test_ambiguous_geography_is_review_not_qualified(self):
        r = classify(
            evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")],
            prelim_overrides={"country": "", "state": ""},
        )
        self.assertEqual(r["final_icp_status"], "review")


class WebsiteUnavailableFallbackTests(unittest.TestCase):
    def test_website_unavailable_high_prelim_confidence_qualifies_medium(self):
        r = classify(no_enrichment=True, prelim_overrides={"icp_confidence": "high"})
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertEqual(r["final_icp_confidence"], "medium")
        self.assertEqual(r["evidence_source"], "preliminary_icp_only")
        self.assertIn(r["final_icp_tier"], ("B", "C", "D"))
        self.assertNotIn(r["final_icp_tier"], ("A", "A+"))

    def test_website_unavailable_medium_prelim_confidence_is_review(self):
        r = classify(no_enrichment=True, prelim_overrides={"icp_confidence": "medium"})
        self.assertEqual(r["final_icp_status"], "review")

    def test_website_status_no_website_triggers_fallback(self):
        r = classify(evidence=[], website_status="no_website", prelim_overrides={"icp_confidence": "high"})
        self.assertEqual(r["final_icp_status"], "qualified")
        self.assertEqual(r["evidence_source"], "preliminary_icp_only")


class EmployeeCountTests(unittest.TestCase):
    def test_missing_employee_count_no_status_impact(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")])
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_small_employee_count_does_not_disqualify(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")], prelim_overrides={"employee_count": 8})
        self.assertEqual(r["final_icp_status"], "qualified")

    def test_employee_count_in_band_adds_score_but_not_required(self):
        without = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")])
        withband = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")], prelim_overrides={"employee_count": 30})
        self.assertGreaterEqual(withband["final_icp_score"], without["final_icp_score"])
        self.assertEqual(withband["final_icp_status"], without["final_icp_status"])


class TierBoundaryTests(unittest.TestCase):
    def _score_row(self, score, status="qualified", n_hvac=2, n_bucket2=1, evidence_source="website_enrichment", website_status="success"):
        return qf.score_to_tier(score, status, n_hvac, n_bucket2, evidence_source, website_status, RULES)

    def test_score_80_is_a_plus_when_gates_pass(self):
        self.assertEqual(self._score_row(80), "A+")

    def test_score_79_is_a(self):
        self.assertEqual(self._score_row(79), "A")

    def test_score_60_is_a(self):
        self.assertEqual(self._score_row(60, n_hvac=1, n_bucket2=0), "A")

    def test_score_59_is_b(self):
        self.assertEqual(self._score_row(59, n_hvac=1, n_bucket2=0), "B")

    def test_score_40_is_b(self):
        self.assertEqual(self._score_row(40, n_hvac=0, n_bucket2=0), "B")

    def test_score_39_is_c(self):
        self.assertEqual(self._score_row(39, n_hvac=0, n_bucket2=0), "C")

    def test_score_15_is_c(self):
        self.assertEqual(self._score_row(15, n_hvac=0, n_bucket2=0), "C")

    def test_score_14_is_d_for_excluded(self):
        self.assertEqual(self._score_row(14, status="excluded"), "D")

    def test_score_14_floors_at_c_for_qualified(self):
        self.assertEqual(self._score_row(14, status="qualified", n_hvac=0, n_bucket2=0), "C")

    def test_score_14_floors_at_c_for_review(self):
        self.assertEqual(self._score_row(14, status="review", n_hvac=0, n_bucket2=0), "C")

    def test_score_0_floors_at_c_for_qualified(self):
        self.assertEqual(self._score_row(0, status="qualified", n_hvac=0, n_bucket2=0), "C")

    def test_a_plus_gate_fails_with_only_one_bucket1_category(self):
        self.assertEqual(self._score_row(90, n_hvac=1, n_bucket2=1), "A")

    def test_a_plus_gate_fails_with_partial_website(self):
        self.assertEqual(self._score_row(90, n_hvac=2, n_bucket2=1, website_status="partial"), "A")

    def test_fallback_cannot_reach_a_plus_or_a(self):
        tier = self._score_row(95, n_hvac=0, n_bucket2=0, evidence_source="preliminary_icp_only", website_status=None)
        self.assertEqual(tier, "B")

    def test_review_record_capped_at_c_even_with_high_score(self):
        self.assertEqual(self._score_row(90, status="review", n_hvac=0, n_bucket2=0), "C")


class ScoreBucketCapTests(unittest.TestCase):
    def test_bucket1_caps_at_45(self):
        cfg = RULES["score"]["bucket1_hvac_direct"]
        self.assertEqual(qf._bucket1_score(6, cfg), 45)
        self.assertEqual(qf._bucket1_score(5, cfg), 45)
        self.assertEqual(qf._bucket1_score(2, cfg), 18)

    def test_bucket2_caps_at_25(self):
        cfg = RULES["score"]["bucket2_service"]
        self.assertEqual(qf._bucket2_score(0, cfg), 0)
        self.assertEqual(qf._bucket2_score(1, cfg), 8)
        self.assertEqual(qf._bucket2_score(2, cfg), 17)
        self.assertEqual(qf._bucket2_score(5, cfg), 25)

    def test_bucket3_caps_at_20(self):
        cfg = RULES["score"]["bucket3_supporting"]
        score = qf._bucket3_score(10, "high", 5.0, 500, cfg)
        self.assertEqual(score, 20)

    def test_score_clamped_0_to_100(self):
        evidence = [make_evidence(c) for c in RULES["hvac_direct_categories"]] + [
            make_evidence(c) for c in RULES["bucket2_service_categories"]
        ]
        r = classify(evidence=evidence, prelim_overrides={"rating": 5.0, "review_count": 1000, "employee_count": 30})
        self.assertLessEqual(r["final_icp_score"], 100)
        self.assertGreaterEqual(r["final_icp_score"], 0)


class DeterminismTests(unittest.TestCase):
    def test_same_input_produces_byte_identical_output(self):
        prelim = make_prelim()
        enrichment = make_enrichment(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")])
        a = qf.qualify_final_record(copy.deepcopy(prelim), copy.deepcopy(enrichment), RULES, FIXED_EVALUATED_AT)
        b = qf.qualify_final_record(copy.deepcopy(prelim), copy.deepcopy(enrichment), RULES, FIXED_EVALUATED_AT)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_output_ordering_by_master_id(self):
        prelim_records = [make_prelim(master_id="bbb"), make_prelim(master_id="aaa")]
        results = qf.qualify_all(prelim_records, {}, RULES, FIXED_EVALUATED_AT)
        self.assertEqual([r["master_id"] for r in results], ["aaa", "bbb"])

    def test_unknown_future_category_does_not_crash(self):
        evidence = [make_evidence("commercial_hvac"), make_evidence("some_future_category_not_in_rules")]
        r = classify(evidence=evidence)
        self.assertEqual(r["final_icp_status"], "qualified")


class DataIntegrityTests(unittest.TestCase):
    def test_run_does_not_mutate_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            master_path = os.path.join(tmp, "master.json")
            prelim_path = os.path.join(tmp, "icp_qualified.json")
            enrichment_path = os.path.join(tmp, "website_enrichment.json")
            out_dir = os.path.join(tmp, "final_icp")

            master_records = [{"master_id": "id1", "business_name": "Acme", "website": "https://acmehvac.com"}]
            prelim_records = [make_prelim(master_id="id1")]
            enrichment_payload = {"stats": {}, "records": [make_enrichment(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")], master_id="id1")]}

            with open(master_path, "w", encoding="utf-8") as f:
                json.dump(master_records, f, indent=2)
            with open(prelim_path, "w", encoding="utf-8") as f:
                json.dump(prelim_records, f, indent=2)
            with open(enrichment_path, "w", encoding="utf-8") as f:
                json.dump(enrichment_payload, f, indent=2)

            master_bytes = open(master_path, "rb").read()
            prelim_bytes = open(prelim_path, "rb").read()
            enrichment_bytes = open(enrichment_path, "rb").read()

            qf.run(master_path, prelim_path, enrichment_path, RULES_PATH, out_dir, evaluated_at=FIXED_EVALUATED_AT)

            self.assertEqual(open(master_path, "rb").read(), master_bytes)
            self.assertEqual(open(prelim_path, "rb").read(), prelim_bytes)
            self.assertEqual(open(enrichment_path, "rb").read(), enrichment_bytes)

    def test_run_writes_to_separate_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            prelim_path = os.path.join(tmp, "icp_qualified.json")
            out_dir = os.path.join(tmp, "final_icp")
            with open(prelim_path, "w", encoding="utf-8") as f:
                json.dump([make_prelim(master_id="id1")], f)

            stats = qf.run(
                os.path.join(tmp, "nonexistent_master.json"), prelim_path,
                os.path.join(tmp, "nonexistent_enrichment.json"), RULES_PATH, out_dir,
                evaluated_at=FIXED_EVALUATED_AT,
            )
            self.assertTrue(os.path.exists(stats["artifacts"]["final_icp_json"]))
            self.assertTrue(os.path.exists(stats["artifacts"]["final_icp_csv"]))
            self.assertEqual(stats["records_evaluated"], 1)

    def test_deterministic_rerun_with_fixed_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            prelim_path = os.path.join(tmp, "icp_qualified.json")
            with open(prelim_path, "w", encoding="utf-8") as f:
                json.dump([make_prelim(master_id="id1"), make_prelim(master_id="id2", business_name="Other Co")], f)

            out_dir_a = os.path.join(tmp, "run_a")
            out_dir_b = os.path.join(tmp, "run_b")
            qf.run(os.path.join(tmp, "no_master.json"), prelim_path, os.path.join(tmp, "no_enrich.json"), RULES_PATH, out_dir_a, evaluated_at=FIXED_EVALUATED_AT)
            qf.run(os.path.join(tmp, "no_master.json"), prelim_path, os.path.join(tmp, "no_enrich.json"), RULES_PATH, out_dir_b, evaluated_at=FIXED_EVALUATED_AT)

            a = open(os.path.join(out_dir_a, "final_icp_qualified.json"), encoding="utf-8").read()
            b = open(os.path.join(out_dir_b, "final_icp_qualified.json"), encoding="utf-8").read()
            self.assertEqual(a, b)


class NoNetworkTests(unittest.TestCase):
    def test_no_network_module_usage(self):
        source = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "final_icp", "qualify_final.py"), encoding="utf-8").read()
        for forbidden in ("requests", "urllib.request", "httpx", "socket.connect", "scrapling"):
            self.assertNotIn(forbidden, source)

    def test_run_with_socket_blocked_still_works(self):
        with mock.patch("socket.socket") as mocked_socket:
            mocked_socket.side_effect = AssertionError("network access attempted")
            with tempfile.TemporaryDirectory() as tmp:
                prelim_path = os.path.join(tmp, "icp_qualified.json")
                with open(prelim_path, "w", encoding="utf-8") as f:
                    json.dump([make_prelim(master_id="id1")], f)
                out_dir = os.path.join(tmp, "final_icp")
                qf.run(
                    os.path.join(tmp, "no_master.json"), prelim_path, os.path.join(tmp, "no_enrich.json"),
                    RULES_PATH, out_dir, evaluated_at=FIXED_EVALUATED_AT,
                )


class GitignoreTests(unittest.TestCase):
    def test_final_icp_output_is_gitignored(self):
        gitignore_path = os.path.join(os.path.dirname(__file__), "..", ".gitignore")
        content = open(gitignore_path, encoding="utf-8").read()
        self.assertIn("data/final_icp/*", content)
        self.assertIn("!data/final_icp/.gitkeep", content)


class OutputSchemaTests(unittest.TestCase):
    def test_required_output_fields_present(self):
        r = classify(evidence=[make_evidence("commercial_hvac"), make_evidence("chiller")])
        for field in qf.OUTPUT_FIELDS:
            self.assertIn(field, r)


if __name__ == "__main__":
    unittest.main()
