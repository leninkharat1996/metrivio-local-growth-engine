import os
import sys
import unittest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "scripts", "google_maps_scraper"),
)

import update_master  # noqa: E402


def make_lead(**overrides):
    base = {
        "business_name": "Joe's HVAC",
        "category": "hvac",
        "address": "123 Main St, Dallas, TX 75201",
        "city": "Dallas",
        "state": "TX",
        "country": "USA",
        "phone": "214-555-0100",
        "website": "https://joeshvac.com",
        "google_maps_url": "https://maps.google.com/?cid=1",
        "rating": 4.5,
        "review_count": 10,
        "latitude": 32.7,
        "longitude": -96.8,
        "source": "google_maps_scraper",
        "scraped_at": "2026-09-09T00:00:00+00:00",
        "search_keyword": "hvac",
        "search_location": "Dallas, TX",
        "search_id": "dallas-tx_hvac",
        "run_id": "run_1",
    }
    base.update(overrides)
    return base


class MasterIdTests(unittest.TestCase):
    def test_stable_across_runs_and_searches(self):
        a = make_lead(run_id="run_1", search_id="dallas-tx_hvac")
        b = make_lead(run_id="run_2", search_id="dallas-tx_hvac_companies", search_keyword="hvac companies")
        self.assertEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_stable_across_cities_when_domain_matches(self):
        a = make_lead(search_location="Dallas, TX")
        b = make_lead(search_location="Fort Worth, TX", address="999 Other Rd, Fort Worth, TX 76102")
        self.assertEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_different_businesses_get_different_ids(self):
        a = make_lead(business_name="Joe's HVAC", website="https://joeshvac.com", phone="")
        b = make_lead(business_name="Ace HVAC", website="https://acehvac.com", phone="")
        self.assertNotEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_matches_by_normalized_domain_ignoring_www_and_scheme(self):
        a = make_lead(website="https://www.joeshvac.com", phone="")
        b = make_lead(website="joeshvac.com", phone="")
        self.assertEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_matches_by_normalized_phone_when_no_website(self):
        a = make_lead(website="", phone="+1 (214) 555-0100")
        b = make_lead(website="", phone="214-555-0100")
        self.assertEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_missing_website_falls_back_to_phone(self):
        a = make_lead(website="", phone="214-555-0100")
        b = make_lead(website="https://unrelated-site.example", phone="214-555-0100")
        # `a` has no website so it matches on phone; `b` has a website so it
        # matches on domain -- these are treated as different identities
        # since domain takes priority whenever present.
        self.assertNotEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_missing_phone_and_website_falls_back_to_name_address(self):
        a = make_lead(website="", phone="")
        b = make_lead(website="", phone="", search_id="other-search", run_id="run_9")
        self.assertEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))

    def test_name_address_fallback_distinguishes_different_addresses(self):
        a = make_lead(website="", phone="", address="123 Main St, Dallas, TX 75201")
        b = make_lead(website="", phone="", address="456 Oak Ave, Dallas, TX 75202")
        self.assertNotEqual(update_master.compute_master_id(a), update_master.compute_master_id(b))


class UpsertTests(unittest.TestCase):
    def test_new_business_creates_master_record(self):
        lead = make_lead()
        records, history, stats = update_master.update_master([lead], {}, set(), [])
        self.assertEqual(stats["new_master_records"], 1)
        self.assertEqual(stats["updated_master_records"], 0)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["first_seen_at"], lead["scraped_at"])
        self.assertEqual(record["last_seen_at"], lead["scraped_at"])
        self.assertEqual(record["source_count"], 1)
        self.assertEqual(record["search_count"], 1)

    def test_reappearing_business_updates_not_duplicates(self):
        first = make_lead(run_id="run_1", search_id="s1", scraped_at="2026-09-01T00:00:00+00:00")
        records, history, _ = update_master.update_master([first], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history}

        second = make_lead(run_id="run_2", search_id="s2", scraped_at="2026-09-05T00:00:00+00:00")
        records2, history2, stats2 = update_master.update_master(
            [second], existing_master, existing_keys, history
        )
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(len(records2), 1)
        record = records2[0]
        self.assertEqual(record["first_seen_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(record["last_seen_at"], "2026-09-05T00:00:00+00:00")
        self.assertEqual(record["search_count"], 2)

    def test_populated_value_not_overwritten_by_blank(self):
        first = make_lead(phone="214-555-0100", website="https://joeshvac.com")
        records, history, _ = update_master.update_master([first], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history}

        second = make_lead(phone="", website="", run_id="run_2", search_id="s2")
        records2, _, _ = update_master.update_master([second], existing_master, existing_keys, history)
        record = records2[0]
        self.assertEqual(record["phone"], "214-555-0100")
        self.assertEqual(record["website"], "https://joeshvac.com")

    def test_blank_value_filled_by_populated_incoming(self):
        # Keep website constant (it's the highest-priority match key) so
        # both leads resolve to the same master_id while phone varies.
        first = make_lead(phone="", website="https://joeshvac.com")
        records, history, _ = update_master.update_master([first], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history}

        second = make_lead(
            phone="214-555-0100", website="https://joeshvac.com", run_id="run_2", search_id="s2"
        )
        records2, _, _ = update_master.update_master([second], existing_master, existing_keys, history)
        record = records2[0]
        self.assertEqual(record["phone"], "214-555-0100")

    def test_conflicting_populated_values_keep_existing(self):
        first = make_lead(rating=4.5, review_count=10)
        records, history, _ = update_master.update_master([first], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history}

        second = make_lead(rating=3.0, review_count=999, run_id="run_2", search_id="s2")
        records2, _, _ = update_master.update_master([second], existing_master, existing_keys, history)
        record = records2[0]
        self.assertEqual(record["rating"], 4.5)
        self.assertEqual(record["review_count"], 10)

    def test_latest_provenance_fields_always_refreshed(self):
        first = make_lead(run_id="run_1", search_id="s1", search_keyword="hvac")
        records, history, _ = update_master.update_master([first], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history}

        second = make_lead(run_id="run_2", search_id="s2", search_keyword="hvac companies")
        records2, _, _ = update_master.update_master([second], existing_master, existing_keys, history)
        record = records2[0]
        self.assertEqual(record["run_id"], "run_2")
        self.assertEqual(record["search_id"], "s2")
        self.assertEqual(record["search_keyword"], "hvac companies")

    def test_genuinely_different_businesses_both_kept(self):
        a = make_lead(business_name="Joe's HVAC", website="https://joeshvac.com")
        b = make_lead(business_name="Ace HVAC", website="https://acehvac.com")
        records, _, stats = update_master.update_master([a, b], {}, set(), [])
        self.assertEqual(stats["new_master_records"], 2)
        self.assertEqual(len(records), 2)


class IdempotencyTests(unittest.TestCase):
    def test_reprocessing_same_dataset_is_idempotent(self):
        leads = [
            make_lead(business_name="Joe's HVAC", website="https://joeshvac.com"),
            make_lead(business_name="Ace HVAC", website="https://acehvac.com", phone="972-555-0200"),
        ]
        records1, history1, stats1 = update_master.update_master(leads, {}, set(), [])
        self.assertEqual(stats1["new_master_records"], 2)
        self.assertEqual(stats1["new_history_events"], 2)

        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        records2, history2, stats2 = update_master.update_master(
            leads, existing_master, existing_keys, history1
        )
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 2)
        self.assertEqual(stats2["new_history_events"], 0)
        self.assertEqual(len(records2), 2)
        self.assertEqual(len(history2), 2)

        # Deterministic output: same master_ids, same field values (aside
        # from any intentionally-refreshed latest-snapshot fields, which are
        # identical here since the reprocessed leads are unchanged).
        by_id_1 = {r["master_id"]: r for r in records1}
        by_id_2 = {r["master_id"]: r for r in records2}
        self.assertEqual(set(by_id_1), set(by_id_2))
        for master_id, rec1 in by_id_1.items():
            rec2 = by_id_2[master_id]
            for field in update_master.CANONICAL_FIELDS:
                self.assertEqual(rec1[field], rec2[field], field)

    def test_discovery_history_deduplicates_same_run_search_pair(self):
        lead = make_lead(run_id="run_1", search_id="s1")
        records1, history1, stats1 = update_master.update_master([lead], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        # Same run_id/search_id reprocessed (e.g. workflow re-run) must not
        # create a second history event.
        records2, history2, stats2 = update_master.update_master(
            [lead], existing_master, existing_keys, history1
        )
        self.assertEqual(stats2["new_history_events"], 0)
        self.assertEqual(len(history2), 1)


class IdentityResolutionTests(unittest.TestCase):
    """Master identity must be resolved against the EXISTING master store
    using all reliable identity keys, not recomputed fresh from whatever
    fields happen to be present on the incoming lead (the Step 6A bug)."""

    def test_website_present_then_missing_keeps_same_master(self):
        run1 = make_lead(website="https://acehvac.com", phone="555-123-4567", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)

    def test_phone_present_then_missing_keeps_same_master(self):
        run1 = make_lead(website="", phone="555-123-4567", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)

    def test_website_missing_first_then_discovered_attaches_to_existing(self):
        run1 = make_lead(website="", phone="", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="https://acehvac.com", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)
        self.assertEqual(records2[0]["website"], "https://acehvac.com")

    def test_phone_missing_first_then_discovered_attaches_to_existing(self):
        run1 = make_lead(website="", phone="", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="555-123-4567", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)
        self.assertEqual(records2[0]["phone"], "555-123-4567")

    def test_resolves_via_website_match(self):
        run1 = make_lead(website="https://acehvac.com", phone="555-000-0001", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="https://www.acehvac.com", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)

    def test_resolves_via_phone_match_when_website_absent(self):
        run1 = make_lead(website="", phone="555-000-0001", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="+1 555 000 0001", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)

    def test_resolves_via_name_address_fallback(self):
        run1 = make_lead(website="", phone="", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)

    def test_multiple_keys_all_pointing_to_same_master_no_conflict(self):
        run1 = make_lead(website="https://acehvac.com", phone="555-000-0001", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id_1 = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        # Same website, same phone, same name/address: all three keys agree.
        run2 = make_lead(website="https://acehvac.com", phone="555-000-0001", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(records2[0]["master_id"], master_id_1)
        self.assertEqual(stats2["new_identity_conflicts"], 0)

    def test_multiple_keys_pointing_to_different_masters_reports_conflict(self):
        a = make_lead(business_name="Ace HVAC", website="https://acehvac.com", phone="555-000-0001", address="1 A St, Dallas, TX 75201", run_id="run_1", search_id="s1")
        b = make_lead(business_name="Beta Air", website="https://betaair.com", phone="555-000-0002", address="2 B St, Dallas, TX 75201", run_id="run_1", search_id="s2")
        records1, history1, _ = update_master.update_master([a, b], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}
        master_a = update_master.compute_master_id(a)
        master_b = update_master.compute_master_id(b)

        # A single incoming record whose website points to master A but
        # whose phone points to master B -- a genuine identity collision.
        colliding = make_lead(business_name="Ace HVAC", website="https://acehvac.com", phone="555-000-0002", address="1 A St, Dallas, TX 75201", run_id="run_2", search_id="s3")
        records2, _, stats2 = update_master.update_master([colliding], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_identity_conflicts"], 1)
        conflict = stats2["identity_conflicts"][-1]
        self.assertEqual(conflict["master_id"], master_a)
        self.assertEqual(conflict["conflicting_master_ids"], master_b)
        # Resolved deterministically via the higher-priority key (domain);
        # never silently merged into a third state.
        by_id = {r["master_id"]: r for r in records2}
        self.assertIn(master_a, by_id)
        self.assertIn(master_b, by_id)

    def test_brand_new_business_with_no_website_or_phone(self):
        lead = make_lead(website="", phone="", business_name="Brand New Biz", address="9 New Rd, Dallas, TX 75201")
        records, _, stats = update_master.update_master([lead], {}, set(), [])
        self.assertEqual(stats["new_master_records"], 1)
        self.assertEqual(len(records), 1)

    def test_stable_master_id_across_three_runs_with_shrinking_fields(self):
        run1 = make_lead(website="https://acehvac.com", phone="555-000-0001", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        master_id = records1[0]["master_id"]
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="555-000-0001", run_id="run_2", search_id="s2")
        records2, history2, _ = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(records2[0]["master_id"], master_id)
        existing_master = {r["master_id"]: r for r in records2}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history2}

        run3 = make_lead(website="", phone="", run_id="run_3", search_id="s3")
        records3, _, stats3 = update_master.update_master([run3], existing_master, existing_keys, history2)
        self.assertEqual(records3[0]["master_id"], master_id)
        self.assertEqual(stats3["new_master_records"], 0)

    def test_no_duplicate_created_when_identifying_fields_disappear(self):
        run1 = make_lead(website="https://acehvac.com", phone="555-000-0001", run_id="run_1", search_id="s1")
        records1, history1, _ = update_master.update_master([run1], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        run2 = make_lead(website="", phone="", run_id="run_2", search_id="s2")
        records2, _, stats2 = update_master.update_master([run2], existing_master, existing_keys, history1)
        self.assertEqual(len(records2), 1)
        self.assertEqual(stats2["total_master_records"], 1)

    def test_idempotent_reprocessing_with_identity_index(self):
        lead = make_lead(website="https://acehvac.com", phone="555-000-0001")
        records1, history1, _ = update_master.update_master([lead], {}, set(), [])
        existing_master = {r["master_id"]: r for r in records1}
        existing_keys = {(h["master_id"], h["run_id"], h["search_id"]) for h in history1}

        records2, _, stats2 = update_master.update_master([lead], existing_master, existing_keys, history1)
        self.assertEqual(stats2["new_master_records"], 0)
        self.assertEqual(stats2["updated_master_records"], 1)
        self.assertEqual(len(records2), 1)

    def test_identity_index_rebuild_from_existing_master_data(self):
        lead = make_lead(website="https://acehvac.com", phone="555-000-0001")
        records, _, _ = update_master.update_master([lead], {}, set(), [])
        master = {r["master_id"]: r for r in records}

        index = update_master.build_identity_index(master)
        self.assertEqual(index[("domain", "acehvac.com")], records[0]["master_id"])
        self.assertEqual(index[("phone", "5550000001")], records[0]["master_id"])
        name_key = update_master.normalize_name_key(lead["business_name"])
        address_key = update_master.normalize_address_key(lead["address"])
        self.assertEqual(index[("name_address", name_key, address_key)], records[0]["master_id"])


class ReadWriteRoundTripTests(unittest.TestCase):
    def test_write_and_reload_master_round_trips(self):
        import tempfile

        lead = make_lead()
        records, history, _ = update_master.update_master([lead], {}, set(), [])
        with tempfile.TemporaryDirectory() as tmpdir:
            update_master.write_master(records, tmpdir)
            update_master.write_history(history, tmpdir)

            reloaded_master = update_master.load_master(os.path.join(tmpdir, "master.json"))
            reloaded_keys = update_master.load_history_keys(os.path.join(tmpdir, "discovery_history.csv"))

            self.assertEqual(len(reloaded_master), 1)
            master_id = update_master.compute_master_id(lead)
            self.assertIn(master_id, reloaded_master)
            self.assertIn((master_id, lead["run_id"], lead["search_id"]), reloaded_keys)


if __name__ == "__main__":
    unittest.main()
