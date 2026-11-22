import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from source_policy import SourcePolicy


class SourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.policy_file = Path(self.temp.name) / "sources.json"
        self.policy = SourcePolicy(self.policy_file)

    def tearDown(self):
        self.temp.cleanup()

    def test_add_and_retrieve_source(self):
        entry = self.policy.add_source(
            "UC-channel-123", "GTAGamerOfficial",
            permission_status="authorized",
            permission_scope=["record", "clip", "publish"],
            proof_reference="Agreement-2026-A",
            credit="Credit: GTAGamerOfficial",
            gameplay_verified=True,
        )
        self.assertEqual(entry["channel_id"], "UC-channel-123")
        self.assertEqual(entry["channel_name"], "GTAGamerOfficial")
        self.assertTrue(self.policy.is_authorized("UC-channel-123", "clip"))
        self.assertTrue(self.policy.is_authorized("UC-channel-123", "publish"))

    def test_unauthorized_when_not_in_allowlist(self):
        self.assertFalse(self.policy.is_authorized("UC-unknown-channel"))

    def test_creator_permission_without_gameplay_review_is_not_enough(self):
        self.policy.add_source("UC-not-reviewed", "NotReviewed")
        self.assertFalse(self.policy.is_authorized("UC-not-reviewed", "record"))

    def test_environment_allowlist_overrides_local_file(self):
        self.policy.add_source("UC-local", "LocalSource", gameplay_verified=True)
        payload = '{"sources":{"UC-env":{"channel_id":"UC-env","channel_name":"EnvSource","permission_status":"authorized","permission_scope":["record","clip"],"gameplay_verified":true}}}'
        with patch.dict("os.environ", {"GTA_SOURCE_ALLOWLIST_JSON": payload}):
            env_policy = SourcePolicy(self.policy_file)
        self.assertTrue(env_policy.is_authorized("UC-env", "record"))
        self.assertFalse(env_policy.is_authorized("UC-local", "record"))

    def test_unauthorized_when_status_is_revoked_or_pending(self):
        self.policy.add_source(
            "UC-pending", "PendingCreator",
            permission_status="pending",
            gameplay_verified=True,
        )
        self.assertFalse(self.policy.is_authorized("UC-pending"))

        self.policy.update_status("UC-pending", "authorized")
        self.assertTrue(self.policy.is_authorized("UC-pending"))

        self.policy.update_status("UC-pending", "revoked")
        self.assertFalse(self.policy.is_authorized("UC-pending"))

    def test_scope_enforcement(self):
        self.policy.add_source(
            "UC-limited", "LimitedCreator",
            permission_status="authorized",
            permission_scope=["record", "clip"],  # no 'publish' scope
            gameplay_verified=True,
        )
        self.assertTrue(self.policy.is_authorized("UC-limited", "clip"))
        self.assertFalse(self.policy.is_authorized("UC-limited", "publish"))

    def test_persistence_across_reopen(self):
        self.policy.add_source("UC-persist", "PersistentGamer", proof_reference="REF-99", gameplay_verified=True)
        reopened = SourcePolicy(self.policy_file)
        self.assertTrue(reopened.is_authorized("UC-persist"))
        src = reopened.get_source("UC-persist")
        self.assertEqual(src["proof_reference"], "REF-99")

    def test_filter_streams(self):
        self.policy.add_source("UC-allowed", "AllowedGamer", gameplay_verified=True)
        streams = [
            {"channel_id": "UC-allowed", "title": "GTA 6 Live Gameplay"},
            {"channel_id": "UC-random", "title": "Random Stream"},
        ]
        authorized, unauthorized = self.policy.filter_streams(streams)
        self.assertEqual(len(authorized), 1)
        self.assertEqual(len(unauthorized), 1)
        self.assertEqual(authorized[0]["channel_id"], "UC-allowed")
        self.assertTrue(authorized[0]["authorized"])
        self.assertEqual(unauthorized[0]["channel_id"], "UC-random")
        self.assertFalse(unauthorized[0]["authorized"])


if __name__ == "__main__":
    unittest.main()
