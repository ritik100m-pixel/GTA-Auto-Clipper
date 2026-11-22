"""Source authorization and copyright safety policy for GTA Auto Clipper Cloud.

Enforces explicit creator permission before streams can be recorded, clipped,
or published. Public availability on YouTube does not imply permission.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SourcePolicy:
    """Manages verified source permissions and allowlists."""

    def __init__(self, policy_path="cloud_state/source_allowlist.json"):
        self.policy_path = Path(policy_path).resolve()
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        self.sources = {}
        self.load()

    def load(self):
        # A repository secret can supply the current source policy without
        # committing creator permissions or proof references to the repo.
        raw_policy = os.environ.get("GTA_SOURCE_ALLOWLIST_JSON", "").strip()
        if raw_policy:
            data = json.loads(raw_policy)
        elif self.policy_path.is_file():
            with open(self.policy_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"sources": {}}

        if not isinstance(data, dict) or not isinstance(data.get("sources", {}), dict):
            raise ValueError("GTA source allowlist must be a JSON object with a 'sources' object")
        sources = data.get("sources", {})
        for channel_id, entry in sources.items():
            if not isinstance(channel_id, str) or not isinstance(entry, dict):
                raise ValueError("Each source allowlist entry must be an object keyed by channel ID")
            if entry.get("channel_id", channel_id) != channel_id:
                raise ValueError(f"Source allowlist key does not match channel_id: {channel_id}")
            if not isinstance(entry.get("permission_scope", []), list):
                raise ValueError(f"permission_scope must be a list for source {channel_id}")
        self.sources = sources
        return self

    def save(self):
        self.policy_path.parent.mkdir(parents=True, exist_ok=True)
        dir_name = self.policy_path.parent
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tf:
            json.dump({
                "version": 1,
                "updated_at": _utc_now(),
                "sources": self.sources,
            }, tf, indent=2, ensure_ascii=False)
            temp_name = tf.name
        os.replace(temp_name, self.policy_path)

    def add_source(self, channel_id, channel_name, *,
                   permission_status="pending",
                   permission_scope=None,
                   proof_reference=None,
                   credit=None,
                   notes=None,
                   gameplay_verified=False):
        """Add or update an authorized source channel."""
        if not channel_id or not str(channel_id).strip():
            raise ValueError("channel_id is required")
        if not channel_name or not str(channel_name).strip():
            raise ValueError("channel_name is required")

        cid = str(channel_id).strip()
        now = _utc_now()
        scope = list(["record", "clip"] if permission_scope is None else permission_scope)

        entry = {
            "channel_id": cid,
            "channel_name": str(channel_name).strip(),
            "permission_status": str(permission_status).strip(),
            "permission_scope": scope,
            "gameplay_verified": bool(gameplay_verified),
            "proof_reference": str(proof_reference or "").strip() or None,
            "credit": str(credit or channel_name).strip(),
            "notes": str(notes or "").strip() or None,
            "added_at": self.sources.get(cid, {}).get("added_at", now),
            "updated_at": now,
        }
        self.sources[cid] = entry
        self.save()
        return entry

    def update_status(self, channel_id, permission_status, *, notes=None):
        cid = str(channel_id).strip()
        if cid not in self.sources:
            raise KeyError(f"source {cid} not found in allowlist")
        self.sources[cid]["permission_status"] = str(permission_status).strip()
        self.sources[cid]["updated_at"] = _utc_now()
        if notes:
            self.sources[cid]["notes"] = str(notes)
        self.save()
        return self.sources[cid]

    def remove_source(self, channel_id):
        cid = str(channel_id).strip()
        if cid in self.sources:
            del self.sources[cid]
            self.save()
            return True
        return False

    def get_source(self, channel_id):
        return self.sources.get(str(channel_id).strip())

    def is_authorized(self, channel_id, required_scope="clip"):
        """Check if a channel has valid, active permission for the requested scope."""
        if not channel_id:
            return False
        entry = self.get_source(channel_id)
        if not entry:
            return False
        status = entry.get("permission_status")
        if status not in {"authorized", "self_owned"}:
            return False
        # Creator permission alone does not establish that the source is GTA
        # gameplay. Require an explicit one-time visual source review as well.
        if entry.get("gameplay_verified") is not True:
            return False
        if status == "authorized" and not (entry.get("proof_reference") or entry.get("notes")):
            return False
        scopes = entry.get("permission_scope", [])
        if required_scope and required_scope not in scopes:
            return False
        return True

    def list_sources(self, status=None):
        if status:
            return [src for src in self.sources.values() if src.get("permission_status") == status]
        return list(self.sources.values())

    def filter_streams(self, streams, required_scope="clip"):
        """Separate streams into authorized and unauthorized lists."""
        authorized = []
        unauthorized = []
        for stream in streams:
            channel_id = stream.get("channel_id")
            if self.is_authorized(channel_id, required_scope=required_scope):
                annotated = dict(stream)
                annotated["source_record"] = self.get_source(channel_id)
                annotated["authorized"] = True
                authorized.append(annotated)
            else:
                annotated = dict(stream)
                annotated["authorized"] = False
                unauthorized.append(annotated)
        return authorized, unauthorized
