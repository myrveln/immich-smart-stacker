from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import immich_smart_stacker.cli as stacker_cli
from tests._module import Asset, SmartStacker, main, unstack_all
import tests._module as mm


class FakeClient:
    def __init__(self):
        self.api_url = "http://x/api"
        self.created = []
        self.deleted = []
        self._stacks = {}

    def get_existing_stacks(self):
        return dict(self._stacks)

    def get_asset_thumbnail(self, asset_id, asset_type=None, skip_video_preview_404=True):
        return object()

    def get_video_frame_from_playback(self, asset_id, ffmpeg_timeout=10.0):
        return None, "ffmpeg-unavailable"

    def create_stack(self, primary, children):
        self.created.append((primary, tuple(children)))
        return True

    def delete_stack(self, stack_id):
        self.deleted.append(stack_id)
        return True

    def get_stacks(self):
        return [
            {"id": "s1", "ownerId": "u1", "assetIds": ["a", "b"]},
            {"id": "s2", "ownerId": "u2", "assetIds": ["x"]},
            {"id": None, "ownerId": "u1", "assetIds": []},
        ]


@pytest.fixture
def sample_assets():
    return [
        Asset("a", "u1", "a.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u1", "b.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("c", "u1", "c.jpg", "2024-01-01T00:10:00Z", "2024-01-01T00:10:00Z", "image"),
    ]


def test_state_file_load_save_and_run_key(tmp_path):
    state_file = tmp_path / "state.json"
    c = FakeClient()
    s = SmartStacker(c, state_file=state_file)
    assert s._build_run_key()

    s.seen_signatures.add("abc")
    s._save_seen_signatures()

    data = json.loads(state_file.read_text())
    assert s.run_key in data["seen"]

    s2 = SmartStacker(c, state_file=state_file, run_key=s.run_key)
    assert "abc" in s2.seen_signatures


def test_run_key_changes_with_scope():
    c = FakeClient()
    s1 = SmartStacker(c, run_scope="u1")
    s2 = SmartStacker(c, run_scope="u2")
    assert s1.run_key != s2.run_key


def test_load_state_failure_is_ignored(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not-json")
    s = SmartStacker(FakeClient(), state_file=f)
    assert isinstance(s.seen_signatures, set)


def test_expand_and_set_helpers(sample_assets):
    c = FakeClient()
    c._stacks = {"s1": ["a", "x"], "s2": ["y"]}
    s = SmartStacker(c)

    expanded = s.expand_with_existing_stacks(["a"])
    assert set(expanded) == {"a", "x"}

    assert s.is_already_stacked(["a", "x"]) is True
    assert s.is_already_stacked(["a", "b"]) is False
    assert SmartStacker._all_in_same_stack(sample_assets[:2]) is False

    same_stack_assets = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image", stackId="s"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image", stackId="s"),
    ]
    assert SmartStacker._all_in_same_stack(same_stack_assets) is True

    s._augment_existing_stacks_from_assets(same_stack_assets)
    assert s.existing_stacks["s"] == ["a", "b"]


def test_compute_hash_paths(monkeypatch, sample_assets):
    c = FakeClient()
    s = SmartStacker(c)

    monkeypatch.setattr(mm.module.imagehash, "average_hash", lambda _img, hash_size=8: "0f")
    assert s.compute_hash(sample_assets[0]) == "0f"

    s.include_videos = False
    video = Asset("v1", "u1", "v.mov", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "video")
    assert s.compute_hash(video) is None

    c.get_asset_thumbnail = lambda *_args, **_kwargs: None
    c.last_thumbnail_status = 403
    assert s.compute_hash(sample_assets[0]) is None
    assert s.inaccessible_assets_count == 1
    assert s.inaccessible_by_user["u1"] == 1
    assert s.inaccessible_by_status["403"] == 1

    def explode(*_args, **_kwargs):
        raise RuntimeError("x")

    c.get_asset_thumbnail = explode
    assert s.compute_hash(sample_assets[0]) is None


def test_compute_hash_video_frame_fallback(monkeypatch):
    c = FakeClient()
    s = SmartStacker(c, include_videos=True, video_frame_fallback=True)

    video = Asset("v1", "u1", "v.mov", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "video")
    c.get_asset_thumbnail = lambda *_args, **_kwargs: None
    c.last_thumbnail_status = 404
    c.get_video_frame_from_playback = lambda *_args, **_kwargs: (object(), "ffmpeg-frame")

    monkeypatch.setattr(mm.module.imagehash, "average_hash", lambda _img, hash_size=8: "0f")
    assert s.compute_hash(video) == "0f"
    assert s.video_events["ffmpeg-frame"] == 1
    assert s.video_events["frame-fallback-used"] == 1


def test_compute_hash_video_preview_unsupported_event():
    c = FakeClient()
    s = SmartStacker(c, include_videos=True, video_frame_fallback=False)

    video = Asset("v1", "u1", "v.mov", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "video")
    c.get_asset_thumbnail = lambda *_args, **_kwargs: None
    c.last_thumbnail_status = 404
    assert s.compute_hash(video) is None
    assert s.video_events["preview-unsupported"] == 1


def test_compute_hash_video_status_event_categorization():
    c = FakeClient()
    s = SmartStacker(c, include_videos=True, video_frame_fallback=False)
    video = Asset("v1", "u1", "v.mov", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "video")

    c.get_asset_thumbnail = lambda *_args, **_kwargs: None

    c.last_thumbnail_status = 403
    assert s.compute_hash(video) is None
    assert s.video_events["thumbnail-access-denied"] == 1

    c.last_thumbnail_status = 500
    assert s.compute_hash(video) is None
    assert s.video_events["thumbnail-http-500"] == 1

    c.last_thumbnail_status = None
    assert s.compute_hash(video) is None
    assert s.video_events["thumbnail-unknown"] == 1


def test_hamming_cluster_and_similarity(sample_assets, monkeypatch):
    c = FakeClient()
    s = SmartStacker(c, temporal_window=2.0)

    assert s.hamming_distance("0f", "0f") == 0
    assert s.hamming_distance(None, "0f") == float("inf")

    clusters = s.cluster_by_temporal_proximity(sample_assets)
    assert len(clusters) == 1

    hashes = {"a": "0f", "b": "0f", "c": "ff"}
    monkeypatch.setattr(s, "compute_hash", lambda asset: hashes[asset.id])
    groups = s.filter_by_visual_similarity(sample_assets, threshold=0)
    assert len(groups) == 1
    assert [a.id for a in groups[0]] == ["a", "b"]


def test_filter_similarity_graph_transitive_connectivity(monkeypatch):
    c = FakeClient()
    s = SmartStacker(c)

    cluster = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("c", "u", "c", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image"),
    ]

    # a~b and b~c are within threshold=1; a~c is not. Graph components should still return [a,b,c].
    hashes = {"a": "0", "b": "1", "c": "3"}
    monkeypatch.setattr(s, "compute_hash", lambda asset: hashes[asset.id])

    groups = s.filter_by_visual_similarity(cluster, threshold=1)
    assert len(groups) == 1
    assert [asset.id for asset in groups[0]] == ["a", "b", "c"]


def test_filter_similarity_graph_requires_two_hashable_assets(monkeypatch):
    c = FakeClient()
    s = SmartStacker(c)

    cluster = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]

    hashes = {"a": "0", "b": None}
    monkeypatch.setattr(s, "compute_hash", lambda asset: hashes[asset.id])

    groups = s.filter_by_visual_similarity(cluster, threshold=1)
    assert groups == []


def test_select_primary_asset_heuristics():
    c = FakeClient()
    s = SmartStacker(c)

    assets = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image", width=3000, height=2000),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image", width=1000, height=800, isFavorite=True),
        Asset("c", "u", "c", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image", width=5000, height=3000),
    ]

    primary = s.select_primary_asset(assets)
    assert primary.id == "b"


def test_select_primary_asset_empty_raises():
    c = FakeClient()
    s = SmartStacker(c)
    with pytest.raises(ValueError):
        s.select_primary_asset([])


def test_run_uses_primary_heuristics(monkeypatch, tmp_path):
    c = FakeClient()
    s = SmartStacker(c, dry_run=False, state_file=tmp_path / "state.json")

    assets = [
        Asset("a", "u1", "a.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image", width=5000, height=3000),
        Asset("b", "u1", "b.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image", width=1000, height=800, isFavorite=True),
    ]

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)

    created = s.run(assets)
    assert created == 1
    assert c.created[0][0] == "b"


def test_cross_user_groups_materialize_only_authenticated_owner_assets(monkeypatch, tmp_path):
    client = FakeClient()
    stacker = SmartStacker(client, dry_run=False, state_file=tmp_path / "cross-user.json")
    assets = [
        Asset("a1", "owner-a", "a1.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("a2", "owner-a", "a2.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("b1", "owner-b", "b1.jpg", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image"),
    ]
    monkeypatch.setattr(stacker, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(stacker, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(stacker, "expand_with_existing_stacks", lambda ids: ids)

    assert stacker.run(assets, stack_owner_id="owner-a") == 1

    assert set(client.created[0][1] + (client.created[0][0],)) == {"a1", "a2"}
    assert "b1" not in client.created[0][1]
    assert stacker.last_run_summary["crossUserGroupsDetected"] == 1
    assert stacker.last_run_summary["crossUserLocalTargets"] == 1


def test_cross_user_groups_skip_when_owner_has_only_one_asset(monkeypatch, tmp_path):
    client = FakeClient()
    stacker = SmartStacker(client, dry_run=False, state_file=tmp_path / "cross-user-single.json")
    assets = [
        Asset("a1", "owner-a", "a1.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b1", "owner-b", "b1.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]
    monkeypatch.setattr(stacker, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(stacker, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(stacker, "expand_with_existing_stacks", lambda ids: ids)

    assert stacker.run(assets, stack_owner_id="owner-a") == 0

    assert client.created == []
    assert stacker.last_run_summary["crossUserGroupsDetected"] == 1
    assert stacker.last_run_summary["crossUserLocalTargets"] == 0


def test_cross_user_mode_skips_same_owner_groups(monkeypatch, tmp_path):
    client = FakeClient()
    stacker = SmartStacker(client, dry_run=False, state_file=tmp_path / "cross-user-same-owner.json")
    assets = [
        Asset("a1", "owner-a", "a1.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("a2", "owner-a", "a2.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]
    monkeypatch.setattr(stacker, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(stacker, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(stacker, "expand_with_existing_stacks", lambda ids: ids)

    assert stacker.run(assets, stack_owner_id="owner-a") == 0

    assert client.created == []
    assert stacker.last_run_summary["crossUserGroupsDetected"] == 0
    assert stacker.last_run_summary["crossUserLocalTargets"] == 0


def test_cross_user_cli_requires_unfiltered_all_users_scope(monkeypatch):
    monkeypatch.delenv("IMMICH_USER_FILTER", raising=False)
    base_args = ["--api-url", "http://x", "--api-key", "key", "--cross-user-shared"]

    assert stacker_cli.main(base_args) == 1
    assert stacker_cli.main(base_args + ["--all-users", "--user-filter", "owner-a"]) == 1
    assert stacker_cli.main(base_args + ["--all-users", "--unstack-all"]) == 1


def test_cross_user_cli_limits_assets_to_authenticated_user_and_shared_partners(monkeypatch):
    monkeypatch.delenv("IMMICH_USER_FILTER", raising=False)
    assets = [
        Asset("a", "owner-a", "a.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "partner-b", "b.jpg", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("c", "unshared-c", "c.jpg", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image"),
    ]
    observed = {}

    class CrossUserClient:
        def __init__(self, _api_url, _api_key):
            pass

        def get_current_user_id(self):
            return "owner-a"

        def get_shared_partner_user_ids(self):
            return ["partner-b"]

        def get_all_assets(self):
            return assets

    class CaptureStacker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, run_assets, stack_owner_id=None):
            observed["assets"] = run_assets
            observed["stack_owner_id"] = stack_owner_id
            return 0

    assert stacker_cli.main(
        ["--api-url", "http://x", "--api-key", "key", "--cross-user-shared", "--all-users"],
        immich_client_cls=CrossUserClient,
        stacker_cls=CaptureStacker,
    ) == 0

    assert [asset.userId for asset in observed["assets"]] == ["owner-a", "partner-b"]
    assert observed["stack_owner_id"] == "owner-a"


def test_cross_user_cli_requires_authenticated_user(monkeypatch):
    monkeypatch.delenv("IMMICH_USER_FILTER", raising=False)

    class UnknownUserClient:
        def __init__(self, _api_url, _api_key):
            pass

        def get_current_user_id(self):
            return None

    assert stacker_cli.main(
        ["--api-url", "http://x", "--api-key", "key", "--cross-user-shared", "--all-users"],
        immich_client_cls=UnknownUserClient,
    ) == 1


def test_merge_replace_and_run_paths(monkeypatch, sample_assets, tmp_path):
    c = FakeClient()
    s = SmartStacker(c, dry_run=True, state_file=tmp_path / "state.json")

    s._replace_overlapping_local_stacks(["x", "y"], "local")
    assert "local" in s.existing_stacks

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [sample_assets[:2]])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [sample_assets[:2]])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)

    created = s.run(sample_assets, user_filter="u1")
    assert created == 1

    s2 = SmartStacker(c, dry_run=False, state_file=tmp_path / "state2.json")
    monkeypatch.setattr(s2, "cluster_by_temporal_proximity", lambda _assets: [sample_assets[:2]])
    monkeypatch.setattr(s2, "filter_by_visual_similarity", lambda _cluster: [sample_assets[:2]])
    monkeypatch.setattr(s2, "expand_with_existing_stacks", lambda ids: ids)

    created2 = s2.run(sample_assets)
    assert created2 == 1
    assert c.created


def test_run_skip_paths(monkeypatch, sample_assets, tmp_path):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "s.json")

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [sample_assets[:2]])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [sample_assets[:2]])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)

    monkeypatch.setattr(s, "is_already_stacked", lambda _ids: True)
    assert s.run(sample_assets) == 0

    monkeypatch.setattr(s, "is_already_stacked", lambda _ids: False)
    monkeypatch.setattr(s, "_all_in_same_stack", lambda _assets: True)
    assert s.run(sample_assets) == 0

    monkeypatch.setattr(s, "_all_in_same_stack", lambda _assets: False)
    sig = SmartStacker._signature(["a", "b"])
    s.seen_signatures.add(sig)
    assert s.run(sample_assets) == 0


def test_run_with_inaccessible_warning_path(monkeypatch, sample_assets, tmp_path):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "s2.json")
    s.inaccessible_assets_count = 1
    s.inaccessible_by_user = {"u1": 1}
    s.inaccessible_by_status = {"404": 1}
    s.video_events = {"preview-unsupported": 1}

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [])
    assert s.run(sample_assets) == 0


def test_run_with_inaccessible_warning_path_user_filtered(monkeypatch, sample_assets, tmp_path, caplog):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "s3.json")
    s.inaccessible_assets_count = 1
    s.inaccessible_by_user = {"u1": 1}
    s.inaccessible_by_status = {"404": 1}

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [])
    with caplog.at_level(mm.module.logging.WARNING):
        assert s.run(sample_assets, user_filter="u1") == 0

    assert "Consider --user-filter <ownerId>" not in caplog.text


def test_unstack_all_paths():
    c = FakeClient()
    assert unstack_all(c, dry_run=True, user_filter="u1") == 1
    assert unstack_all(c, dry_run=False, user_filter=None) == 2

    c2 = FakeClient()
    c2.get_stacks = lambda: []
    assert unstack_all(c2) == 0


def test_main_paths(monkeypatch):
    monkeypatch.delenv("IMMICH_USER_FILTER", raising=False)

    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()
            self.api_url = api_url
            self.api_key = api_key

        def get_current_user_id(self):
            return "u1"

        def get_all_assets(self):
            return [
                Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
                Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
            ]

    class MainStacker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, assets, user_filter=None):
            return 7

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)

    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k"])  # nosec B106
    assert main() == 0

    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", " "])
    assert main() == 1

    class EmptyClient(MainClient):
        def get_all_assets(self):
            return []

    monkeypatch.setattr(mm.module, "ImmichClient", EmptyClient)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k"])  # nosec B106
    assert main() == 1

    class FailClient(MainClient):
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(mm.module, "ImmichClient", FailClient)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k"])  # nosec B106
    assert main() == 1


def test_cli_main_processes_assets_when_current_user_is_unavailable(monkeypatch):
    monkeypatch.delenv("IMMICH_USER_FILTER", raising=False)

    class UnknownUserClient:
        def __init__(self, _api_url, _api_key):
            pass

        def get_current_user_id(self):
            return None

        def get_all_assets(self):
            return [Asset("a", "u1", "a.jpg", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image")]

    class NoopStacker:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, _assets, user_filter=None):
            return 0

    assert stacker_cli.main(
        ["--api-url", "http://x", "--api-key", "key"],
        immich_client_cls=UnknownUserClient,
        stacker_cls=NoopStacker,
    ) == 0


def test_main_unstack_mode(monkeypatch):
    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "unstack_all", lambda client, dry_run, user_filter: 3)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--unstack-all"])  # nosec B106
    assert main() == 0


def test_main_scheduled_mode_success(monkeypatch):
    run_calls = {"count": 0}
    sleeps = []

    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()

        def get_current_user_id(self):
            return "u1"

        def get_all_assets(self):
            return [
                Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
                Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
            ]

    class MainStacker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, assets, user_filter=None):
            run_calls["count"] += 1
            return 0

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)
    monkeypatch.setattr(mm.module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        [
            "prog",
            "--api-url",
            "http://x",
            "--api-key",
            "k",
            "--interval-seconds",
            "0.01",
            "--max-runs",
            "2",
        ],
    )
    assert main() == 0
    assert run_calls["count"] == 2
    assert sleeps == [0.01]


def test_main_scheduled_mode_failure(monkeypatch):
    sleeps = []

    class FailClient(FakeClient):
        def __init__(self, api_url, api_key):
            raise RuntimeError("boom")

    monkeypatch.setattr(mm.module, "ImmichClient", FailClient)
    monkeypatch.setattr(mm.module.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        [
            "prog",
            "--api-url",
            "http://x",
            "--api-key",
            "k",
            "--interval-seconds",
            "0.01",
            "--max-runs",
            "2",
        ],
    )
    assert main() == 1
    assert sleeps == [0.01]


def test_main_scheduled_mode_invalid_args(monkeypatch):
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--interval-seconds", "-1"])
    assert main() == 1

    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        ["prog", "--api-url", "http://x", "--api-key", "k", "--interval-seconds", "1", "--max-runs", "0"],
    )
    assert main() == 1


def test_datetime_and_state_helpers_roundtrip(tmp_path):
    parsed = mm.module._parse_datetime_arg("2026-07-08T12:00:00Z", "--since")
    assert parsed.tzinfo is not None
    assert mm.module._format_datetime_utc(parsed) == "2026-07-08T12:00:00Z"
    assert mm.module._format_datetime_utc(None) is None

    naive = mm.module._parse_datetime_arg("2026-07-08T12:00:00", "--since")
    assert naive.tzinfo is not None

    with pytest.raises(ValueError):
        mm.module._parse_datetime_arg("bad", "--since")

    state_file = tmp_path / "state.json"
    assert mm.module._load_state_json(state_file) == {}

    state_file.write_text("[]")
    assert mm.module._load_state_json(state_file) == {}

    state_file.write_text("{not-json")
    assert mm.module._load_state_json(state_file) == {}

    mm.module._save_state_json(state_file, {"seen": {}})
    assert mm.module._load_state_json(state_file) == {"seen": {}}

    watermark_key = "rk"
    dt = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)
    mm.module._save_watermark(state_file, watermark_key, dt)
    loaded = mm.module._load_watermark(state_file, watermark_key)
    assert loaded == dt
    assert mm.module._load_watermark(state_file, "missing") is None

    data = json.loads(state_file.read_text())
    data["watermarks"][watermark_key] = "invalid"
    state_file.write_text(json.dumps(data))
    assert mm.module._load_watermark(state_file, watermark_key) is None


def test_apply_time_window_filter(sample_assets):
    c = FakeClient()
    since = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    until = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    s = SmartStacker(c, since_dt=since, until_dt=until)

    filtered = s._apply_time_window_filter(sample_assets)
    assert [a.id for a in filtered] == ["b"]


def test_run_populates_summary_for_empty_after_time_filter(sample_assets):
    c = FakeClient()
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    s = SmartStacker(c, since_dt=since)
    assert s.run(sample_assets) == 0
    assert s.last_run_summary["assetsAfterTimeFilter"] == 0
    assert s.last_run_summary["stacksCreated"] == 0


def test_main_validation_errors(monkeypatch):
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--last-n-days", "-1"])
    assert main() == 1

    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--since", "bad"])
    assert main() == 1

    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        [
            "prog",
            "--api-url",
            "http://x",
            "--api-key",
            "k",
            "--since",
            "2026-07-09T00:00:00Z",
            "--until",
            "2026-07-08T00:00:00Z",
        ],
    )
    assert main() == 1


def test_main_output_json_and_watermark(monkeypatch, tmp_path, capsys):
    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()
            self.api_url = api_url
            self.api_key = api_key

        def get_current_user_id(self):
            return "u1"

        def get_all_assets(self):
            return [
                Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
                Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
            ]

    class MainStacker:
        def __init__(self, *args, **kwargs):
            self.run_key = "rk"
            self.since_dt = kwargs.get("since_dt")
            self.until_dt = kwargs.get("until_dt")
            self.last_run_summary = {
                "inputAssetsTotal": 2,
                "assetsAfterUserFilter": 2,
                "assetsAfterTimeFilter": 2,
                "temporalClusters": 1,
                "candidateGroups": 1,
                "disjointTargets": 1,
                "stacksCreated": 1,
                "inaccessibleAssets": 0,
                "inaccessibleByUser": {},
                "inaccessibleByStatus": {},
                "videoEvents": {},
                "durationSeconds": 0.1,
            }
            self.last_processed_max_created_dt = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)

        def run(self, assets, user_filter=None):
            return 1

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"watermarks": {"rk": "2024-01-01T00:00:00Z"}}))

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)
    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        [
            "prog",
            "--api-url",
            "http://x",
            "--api-key",
            "k",
            "--state-file",
            str(state_file),
            "--use-watermark",
            "--output-json",
        ],
    )
    assert main() == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["summary"]["stacksCreated"] == 1
    assert payload["watermark"]["loaded"] == "2024-01-01T00:00:00Z"
    assert payload["watermark"]["saved"] == "2024-01-01T00:00:01Z"


def test_main_output_json_error_branch(monkeypatch, capsys):
    class FailClient(FakeClient):
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(mm.module, "ImmichClient", FailClient)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--output-json"])
    assert main() == 1

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "error"


def test_main_last_n_days_overrides_since(monkeypatch, tmp_path):
    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()

        def get_current_user_id(self):
            return "u1"

        def get_all_assets(self):
            return [Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image")]

    captured = {}

    class MainStacker:
        def __init__(self, *args, **kwargs):
            captured["since_dt"] = kwargs.get("since_dt")
            self.run_key = "rk2"
            self.since_dt = kwargs.get("since_dt")
            self.until_dt = kwargs.get("until_dt")
            self.last_processed_max_created_dt = None
            self.last_run_summary = {}

        def run(self, assets, user_filter=None):
            return 0

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)
    monkeypatch.setattr(
        mm.module.sys,
        "argv",
        [
            "prog",
            "--api-url",
            "http://x",
            "--api-key",
            "k",
            "--state-file",
            str(tmp_path / "state.json"),
            "--since",
            "2020-01-01T00:00:00Z",
            "--last-n-days",
            "1",
        ],
    )

    assert main() == 0
    assert captured["since_dt"] is not None
    assert captured["since_dt"] > datetime.now(timezone.utc) - timedelta(days=2)


def test_resolve_profiles_user_filter_shorthand():
    args = stacker_cli.parse_args(
        [
            "--api-url",
            "http://x",
            "--api-key",
            "key",
            "--user-filters",
            "u1, u2",
        ]
    )

    profiles = stacker_cli._resolve_profiles(args)

    assert [profile.user_filter for profile in profiles] == ["u1", "u2"]
    assert [profile.profile_name for profile in profiles] == ["user-u1", "user-u2"]


@pytest.mark.parametrize(
    ("config", "options", "message"),
    [
        ([], [], "root object"),
        ({"profiles": []}, [], "at least one profile"),
        ({"profiles": [None]}, [], "must be an object"),
        ({"profiles": [{"name": "p", "unknown": True}]}, [], "unsupported field"),
        ({"profiles": [{"name": " "}]}, [], "non-empty name"),
        (
            {"profiles": [{"name": "p"}, {"name": "p"}]},
            ["--api-url", "http://x", "--api-key", "key"],
            "Duplicate profile name",
        ),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "apiKeyEnv": "KEY"}]},
            [],
            "cannot set both",
        ),
        ({"profiles": [{"name": "p", "apiKeyEnv": " "}]}, [], "apiKeyEnv must be"),
        ({"profiles": [{"name": "p", "apiKey": 42}]}, [], "apiKey must be a string"),
        ({"profiles": [{"name": "p", "apiUrl": "", "apiKey": "key"}]}, [], "requires apiUrl"),
        ({"profiles": [{"name": "p", "apiUrl": "http://x"}]}, [], "requires apiKey"),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "userFilter": 7}]},
            [],
            "userFilter must be",
        ),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "userFilter": "u1", "allUsers": True}]},
            [],
            "cannot set both",
        ),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "includeVideos": "yes"}]},
            [],
            "includeVideos must be a boolean",
        ),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "temporalWindow": 0}]},
            [],
            "temporalWindow must be a positive number",
        ),
        (
            {"profiles": [{"name": "p", "apiUrl": "http://x", "apiKey": "key", "hashThreshold": -1}]},
            [],
            "hashThreshold must be a non-negative integer",
        ),
    ],
)
def test_resolve_profiles_rejects_invalid_config(monkeypatch, tmp_path, config, options, message):
    monkeypatch.delenv("IMMICH_API_URL", raising=False)
    monkeypatch.delenv("IMMICH_API_KEY", raising=False)
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps(config))
    args = stacker_cli.parse_args(["--profiles-file", str(profile_file), *options])

    with pytest.raises(ValueError, match=message):
        stacker_cli._resolve_profiles(args)


@pytest.mark.parametrize(
    ("user_filters", "message"),
    [("u1,,u2", "non-empty user IDs"), ("u1,u1", "duplicate user IDs")],
)
def test_resolve_profiles_rejects_invalid_user_filter_lists(user_filters, message):
    args = stacker_cli.parse_args(["--api-url", "http://x", "--api-key", "key", "--user-filters", user_filters])

    with pytest.raises(ValueError, match=message):
        stacker_cli._resolve_profiles(args)


def test_resolve_profiles_rejects_invalid_files(tmp_path):
    missing_args = stacker_cli.parse_args(["--profiles-file", str(tmp_path / "missing.json")])
    with pytest.raises(ValueError, match="Could not read profiles file"):
        stacker_cli._resolve_profiles(missing_args)

    malformed_file = tmp_path / "malformed.json"
    malformed_file.write_text("{")
    malformed_args = stacker_cli.parse_args(["--profiles-file", str(malformed_file)])
    with pytest.raises(ValueError, match="Could not read profiles file"):
        stacker_cli._resolve_profiles(malformed_args)


def test_resolve_profiles_resets_inherited_user_filter_for_all_users(tmp_path):
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(json.dumps({"profiles": [{"name": "all", "allUsers": True}]}))
    args = stacker_cli.parse_args(
        ["--profiles-file", str(profile_file), "--api-url", "http://x", "--api-key", "key", "--user-filter", "u1"]
    )

    profiles = stacker_cli._resolve_profiles(args)

    assert profiles[0].user_filter is None


def test_main_profiles_continue_after_failure_and_aggregate(monkeypatch, tmp_path, capsys):
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text(
        json.dumps(
            {
                "profiles": [
                    {"name": "failed", "apiUrl": "http://x", "apiKeyEnv": "FAIL_KEY", "userFilter": "u1"},
                    {"name": "first", "apiUrl": "http://x", "apiKeyEnv": "GOOD_KEY", "userFilter": "u1"},
                    {"name": "second", "apiUrl": "http://x", "apiKey": "${GOOD_KEY}", "userFilter": "u1"},
                ]
            }
        )
    )
    monkeypatch.setenv("FAIL_KEY", "bad")
    monkeypatch.setenv("GOOD_KEY", "good")
    run_scopes = []

    class BatchClient(FakeClient):
        def __init__(self, api_url, api_key):
            if api_key == "bad":
                raise RuntimeError("profile unavailable")
            super().__init__()

        def get_all_assets(self):
            return [
                Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
                Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
            ]

    class BatchStacker:
        def __init__(self, _client, **kwargs):
            run_scopes.append(kwargs["run_scope"])
            self.last_run_summary = {
                "stacksCreated": 1,
                "assetsAfterUserFilter": 2,
                "videoEvents": {"unsupported": 1},
            }

        def run(self, _assets, user_filter=None):
            return 1

    result = stacker_cli.main(
        ["--profiles-file", str(profile_file), "--output-json"],
        immich_client_cls=BatchClient,
        stacker_cls=BatchStacker,
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["profileCount"] == 3
    assert payload["failedProfiles"] == 1
    assert [profile["status"] for profile in payload["profiles"]] == ["error", "ok", "ok"]
    assert payload["summary"]["stacksCreated"] == 2
    assert payload["summary"]["videoEvents"] == {"unsupported": 2}
    assert len(run_scopes) == 2
    assert run_scopes[0] != run_scopes[1]


def test_main_reports_missing_api_url(monkeypatch):
    monkeypatch.setenv("IMMICH_API_URL", "")

    assert stacker_cli.main(["--api-key", "key"]) == 1


def test_main_reports_profile_resolution_error_as_json(tmp_path, capsys):
    profile_file = tmp_path / "profiles.json"
    profile_file.write_text("{")

    assert stacker_cli.main(["--profiles-file", str(profile_file), "--output-json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["error"].startswith("Could not read profiles file:")


def test_profile_batch_records_early_run_failure_and_logs_summary():
    result = stacker_cli.main(
        [
            "--api-url",
            "http://x",
            "--api-key",
            "key",
            "--user-filters",
            "u1",
            "--interval-seconds",
            "-1",
        ]
    )

    assert result == 1
