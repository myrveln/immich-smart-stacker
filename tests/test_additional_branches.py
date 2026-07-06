from __future__ import annotations

from pathlib import Path

import tests._module as mm
from tests._module import Asset, ImmichClient, SmartStacker, main


class DummyResp:
    def __init__(self, status_code=200, payload=None, text="", raises=None):
        self.status_code = status_code
        self._payload = {} if payload is None else payload
        self.text = text
        self._raises = raises
        self.content = b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self._raises:
            raise self._raises


class DummySession:
    def __init__(self):
        self.headers = {}
        self.post_responses = []
        self.get_responses = []

    def post(self, _url, json=None):
        return self.post_responses.pop(0)

    def get(self, _url, params=None):
        return self.get_responses.pop(0)


class FakeClient:
    def __init__(self):
        self.api_url = "http://x/api"
        self._stacks = {"s-empty": [], "s1": ["a", "b"]}
        self.created = []

    def get_existing_stacks(self):
        return dict(self._stacks)

    def get_asset_thumbnail(self, _asset_id):
        return None

    def create_stack(self, primary, children):
        self.created.append((primary, tuple(children)))
        return False


def test_search_metadata_error_body_variants():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=400, payload={"errors": ["bad"]}, raises=RuntimeError("bad"))]
    c.session = s
    try:
        c._search_metadata({"query": "x"})
    except RuntimeError:
        pass

    c2 = ImmichClient("http://x", "k")
    s2 = DummySession()
    s2.post_responses = [DummyResp(status_code=400, payload={"message": "oops"}, raises=RuntimeError("bad"))]
    c2.session = s2
    try:
        c2._search_metadata({"query": "x"})
    except RuntimeError:
        pass


def test_search_metadata_400_return_path_without_raise():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=400, payload={"message": "x"})]
    c.session = s
    r = c._search_metadata({"query": "x"})
    assert r.status_code == 400


def test_search_metadata_get_fallback_405():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=405)]
    s.get_responses = [DummyResp(status_code=200, payload={"assets": []})]
    c.session = s
    assert c._search_metadata({"page": 1, "size": 2}).status_code == 200


def test_get_all_assets_branches(monkeypatch):
    c = ImmichClient("http://x", "k")
    pages = [
        DummyResp(
            status_code=200,
            payload={
                "assets": {
                    "items": [
                        {"id": None},
                        {
                            "id": "a",
                            "fileCreatedAt": "2024-01-01T00:00:00Z",
                            "ownerId": "u1",
                            "type": "IMAGE",
                        },
                    ],
                    "nextPage": "oops",
                }
            },
        ),
        DummyResp(status_code=200, payload={"assets": []}),
    ]

    def fake_search(_payload):
        return pages.pop(0)

    monkeypatch.setattr(c, "_search_metadata", fake_search)
    assets = c.get_all_assets()
    assert [a.id for a in assets] == ["a"]


def test_get_all_assets_sequential_page_increment(monkeypatch):
    c = ImmichClient("http://x", "k")
    full = [
        {
            "id": f"a{i}",
            "fileCreatedAt": "2024-01-01T00:00:00Z",
            "ownerId": "u1",
            "type": "IMAGE",
        }
        for i in range(250)
    ]
    pages = [
        DummyResp(status_code=200, payload={"assets": {"items": full, "nextPage": None}}),
        DummyResp(status_code=200, payload={"assets": []}),
    ]

    def fake_search(_payload):
        return pages.pop(0)

    monkeypatch.setattr(c, "_search_metadata", fake_search)
    assets = c.get_all_assets()
    assert len(assets) == 250


def test_thumbnail_permission_json_error_and_404_both():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=401, payload=ValueError("bad"))]
    c.session = s
    assert c.get_asset_thumbnail("x") is None

    c2 = ImmichClient("http://x", "k")
    s2 = DummySession()
    s2.get_responses = [DummyResp(status_code=404), DummyResp(status_code=404)]
    c2.session = s2
    assert c2.get_asset_thumbnail("x") is None


def test_thumbnail_permission_warning_generic_message_and_500_no_raise():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=403, payload={"message": "other issue"})]
    c.session = s
    assert c.get_asset_thumbnail("x") is None

    c2 = ImmichClient("http://x", "k")
    s2 = DummySession()
    s2.get_responses = [DummyResp(status_code=500, payload={})]
    c2.session = s2
    assert c2.get_asset_thumbnail("x") is None


def test_state_save_failure_path(tmp_path):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "missing" / "state.json")
    s.seen_signatures.add("x")
    s._save_seen_signatures()


def test_state_save_none_and_existing_file_path(tmp_path):
    s_none = SmartStacker(FakeClient(), state_file=None)
    s_none._save_seen_signatures()

    state_file = tmp_path / "state.json"
    state_file.write_text('{"seen": {}}')
    s = SmartStacker(FakeClient(), state_file=state_file)
    s.seen_signatures.add("abc")
    s._save_seen_signatures()
    assert "abc" in state_file.read_text()


def test_helpers_and_run_skips(tmp_path, monkeypatch):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "s.json")

    assert SmartStacker._merge_overlapping_sets([]) == []

    assets = [
        Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)

    # create_stack returns False in FakeClient, exercising that branch
    assert s.run(assets) == 0


def test_more_stacker_branches(monkeypatch, tmp_path):
    c = FakeClient()
    s = SmartStacker(c, state_file=tmp_path / "s3.json")

    # _to_asset missing timestamp path
    assert ImmichClient._to_asset({"id": "x", "fileName": "x.jpg"}) is None

    # get_existing_stacks continue path for missing ids
    c2 = FakeClient()
    c2.get_existing_stacks = lambda: {"ok": ["b"]}
    s2 = SmartStacker(c2, state_file=tmp_path / "s4.json")
    assert "ok" in s2.existing_stacks

    # expand_with_existing_stacks empty stack branch
    assert "a" in s.expand_with_existing_stacks(["a"])

    # compute_hash logged==10 branch
    s.inaccessible_assets_logged = 10
    assert s.compute_hash(Asset("z", "u", "z", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image")) is None

    # cluster final append branch
    two = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]
    assert len(s.cluster_by_temporal_proximity(two)) == 1

    # filter threshold default and used-skip inner branch
    hashes = {"a": "00", "b": "ff", "c": "00"}
    tri = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("c", "u", "c", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image"),
    ]
    monkeypatch.setattr(s, "compute_hash", lambda asset: hashes[asset.id])
    s.filter_by_visual_similarity(tri)

    # overlap deletion branch
    s.existing_stacks = {"old": ["a", "x"], "other": ["z"]}
    s._replace_overlapping_local_stacks(["a", "b"], "local")
    assert "old" not in s.existing_stacks

    # run no-assets branch
    assert s.run([]) == 0

    # run group-size skip and expanded-assets missing branch and merged-loop skips
    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [[tri[0]], tri])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda cluster: [[cluster[0]]])
    assert s.run(tri) == 0

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [tri])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [tri])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda _ids: ["missing-id"])
    assert s.run(tri) == 0

    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)
    monkeypatch.setattr(s, "is_already_stacked", lambda _ids: True)
    assert s.run(tri) == 0

    monkeypatch.setattr(s, "is_already_stacked", lambda _ids: False)
    monkeypatch.setattr(s, "_all_in_same_stack", lambda _assets: False)
    s.seen_signatures.add(SmartStacker._signature([a.id for a in tri]))
    assert s.run(tri) == 0

    # inaccessible ratio warning path
    s.inaccessible_assets_count = 1
    s.inaccessible_by_user = {"u": 1}
    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [])
    assert s.run(tri[:1]) == 0


def test_main_verbose_and_all_users(monkeypatch, tmp_path):
    class MainClient:
        def __init__(self, api_url, api_key):
            self.api_url = api_url
            self.api_key = api_key

        def get_all_assets(self):
            return [
                Asset("a", "u1", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
                Asset("b", "u1", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
            ]

        def get_current_user_id(self):
            raise AssertionError("should not be called when --all-users")

        def get_existing_stacks(self):
            return {}

    class MainStacker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _assets, user_filter=None):
            return 0

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)
    monkeypatch.setenv("VERBOSE", "true")
    monkeypatch.setattr(mm.module.sys, "argv", [
        "prog",
        "--api-url",
        "http://x",
        "--api-key",
        "k",
        "--all-users",
        "--verbose",
        "--state-file",
        str(tmp_path / "state.json"),
    ])
    assert main() == 0


def test_main_no_current_user_found(monkeypatch):
    class MainClient:
        def __init__(self, api_url, api_key):
            pass

        def get_current_user_id(self):
            return None

        def get_all_assets(self):
            return [Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image")]

        def get_existing_stacks(self):
            return {}

    class MainStacker:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, _assets, user_filter=None):
            return 0

    monkeypatch.setattr(mm.module, "ImmichClient", MainClient)
    monkeypatch.setattr(mm.module, "SmartStacker", MainStacker)
    monkeypatch.setattr(mm.module.sys, "argv", ["prog", "--api-url", "http://x", "--api-key", "k"])
    assert main() == 0


def test_get_existing_stacks_skips_missing_ids(monkeypatch):
    c = ImmichClient("http://x", "k")
    monkeypatch.setattr(
        c,
        "get_stacks",
        lambda: [{"id": None, "assetIds": ["a"]}, {"id": "ok", "assetIds": ["b"]}],
    )
    assert c.get_existing_stacks() == {"ok": ["b"]}


def test_filter_similarity_inner_used_continue(monkeypatch, tmp_path):
    s = SmartStacker(FakeClient(), state_file=tmp_path / "s5.json")
    cluster = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
        Asset("c", "u", "c", "2024-01-01T00:00:02Z", "2024-01-01T00:00:02Z", "image"),
    ]
    hashes = {"a": "00", "b": "ff", "c": "00"}
    monkeypatch.setattr(s, "compute_hash", lambda asset: hashes[asset.id])
    groups = s.filter_by_visual_similarity(cluster, threshold=0)
    assert groups


def test_run_second_loop_skip_and_short_group(monkeypatch, tmp_path):
    s = SmartStacker(FakeClient(), state_file=tmp_path / "s6.json")
    assets = [
        Asset("a", "u", "a", "2024-01-01T00:00:00Z", "2024-01-01T00:00:00Z", "image"),
        Asset("b", "u", "b", "2024-01-01T00:00:01Z", "2024-01-01T00:00:01Z", "image"),
    ]

    monkeypatch.setattr(s, "cluster_by_temporal_proximity", lambda _assets: [assets])
    monkeypatch.setattr(s, "filter_by_visual_similarity", lambda _cluster: [assets])
    monkeypatch.setattr(s, "expand_with_existing_stacks", lambda ids: ids)

    # First call (inside first loop) false, second call (inside merged loop) true -> line 671 path.
    seen = {"count": 0}

    def staged_is_stacked(_ids):
        seen["count"] += 1
        return seen["count"] >= 2

    monkeypatch.setattr(s, "is_already_stacked", staged_is_stacked)
    assert s.run(assets) == 0

    # Force merged loop to see <2 resolvable assets -> line 676 path.
    monkeypatch.setattr(s, "is_already_stacked", lambda _ids: False)
    monkeypatch.setattr(s, "_merge_overlapping_sets", lambda _groups: [{"missing-id"}])
    assert s.run(assets) == 0
