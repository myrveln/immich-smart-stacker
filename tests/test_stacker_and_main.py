from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_unstack_all_paths():
    c = FakeClient()
    assert unstack_all(c, dry_run=True, user_filter="u1") == 1
    assert unstack_all(c, dry_run=False, user_filter=None) == 2

    c2 = FakeClient()
    c2.get_stacks = lambda: []
    assert unstack_all(c2) == 0


def test_main_paths(monkeypatch):
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


def test_main_unstack_mode(monkeypatch):
    class MainClient(FakeClient):
        def __init__(self, api_url, api_key):
            super().__init__()

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "unstack_all", lambda client, dry_run, user_filter: 3)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k", "--unstack-all"])  # nosec B106
    assert main() == 0
