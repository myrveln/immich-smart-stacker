from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

import tests._module as mm
from tests._module import Asset, ImmichClient


class DummyResp:
    def __init__(self, status_code=200, payload=None, text="", raises=None, content=b"", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self._raises = raises
        self.content = content
        self.headers = headers or {}

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
        self.delete_responses = []
        self.post_calls = []
        self.get_calls = []
        self.delete_calls = []

    def post(self, url, json=None):
        self.post_calls.append((url, json))
        return self.post_responses.pop(0)

    def get(self, url, params=None):
        self.get_calls.append((url, params))
        return self.get_responses.pop(0)

    def delete(self, url):
        self.delete_calls.append(url)
        return self.delete_responses.pop(0)

    def request(self, method, url, timeout=None, **kwargs):
        method_upper = method.upper()
        if method_upper == "POST":
            return self.post(url, json=kwargs.get("json"))
        if method_upper == "GET":
            return self.get(url, params=kwargs.get("params"))
        if method_upper == "DELETE":
            return self.delete(url)
        raise AssertionError(f"Unsupported method in test double: {method}")


def make_image_bytes():
    im = Image.new("RGB", (2, 2), color="red")
    buf = BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_asset_helpers():
    a = Asset(
        id="a",
        userId="u",
        fileName="f",
        fileCreatedAt="2024-01-01T00:00:00Z",
        updatedAt="2024-01-01T00:00:00Z",
        type="image",
    )
    assert a.created_dt.year == 2024
    assert "Asset(" in repr(a)


def test_search_metadata_success_first_try():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=200, payload={"ok": True})]
    c.session = s

    r = c._search_metadata({"page": 1, "size": 1})
    assert r.status_code == 200


def test_search_metadata_retries_skip_take_removal():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [
        DummyResp(status_code=400, payload={"errors": ["bad"]}),
        DummyResp(status_code=200, payload={"ok": True}),
    ]
    c.session = s

    r = c._search_metadata({"page": 1, "size": 10, "skip": 0, "take": 10})
    assert r.status_code == 200
    assert len(s.post_calls) == 2


def test_search_metadata_retries_legacy_skip_take_addition():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [
        DummyResp(status_code=400, payload={"message": "bad"}),
        DummyResp(status_code=200, payload={"ok": True}),
    ]
    c.session = s

    r = c._search_metadata({"page": "2", "size": "50"})
    assert r.status_code == 200
    assert s.post_calls[1][1]["skip"] == 50


def test_search_metadata_get_fallback_on_404():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=404, payload={})]
    s.get_responses = [DummyResp(status_code=200, payload={"assets": []})]
    c.session = s

    r = c._search_metadata({"page": 1, "size": 1})
    assert r.status_code == 200


def test_request_retries_retryable_status(monkeypatch):
    c = ImmichClient("http://x", "k", max_retries=1, retry_backoff=0)
    s = DummySession()
    s.post_responses = [DummyResp(status_code=503), DummyResp(status_code=200, payload={"ok": True})]
    c.session = s

    monkeypatch.setattr("tests._module.module.time.sleep", lambda *_args, **_kwargs: None)
    resp = c._search_metadata({"page": 1, "size": 1})
    assert resp.status_code == 200
    assert len(s.post_calls) == 2


def test_request_retry_after_header(monkeypatch):
    c = ImmichClient("http://x", "k", max_retries=1, retry_backoff=0.1)
    s = DummySession()
    s.post_responses = [
        DummyResp(status_code=429, headers={"Retry-After": "2"}),
        DummyResp(status_code=200, payload={"ok": True}),
    ]
    c.session = s

    sleeps = []
    monkeypatch.setattr("tests._module.module.time.sleep", lambda delay: sleeps.append(delay))
    resp = c._search_metadata({"page": 1, "size": 1})
    assert resp.status_code == 200
    assert sleeps == [2.0]


def test_request_exception_retries_then_succeeds(monkeypatch):
    c = ImmichClient("http://x", "k", max_retries=1, retry_backoff=0.25)

    class RaisingSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, method, url, timeout=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise mm.module.requests.RequestException("temporary")
            return DummyResp(status_code=200, payload={"ok": True})

    s = RaisingSession()
    c.session = s

    sleeps = []
    monkeypatch.setattr("tests._module.module.time.sleep", lambda delay: sleeps.append(delay))
    resp = c._request("GET", "http://x/api/ping")
    assert resp.status_code == 200
    assert sleeps == [0.25]


def test_request_exception_raises_on_last_attempt():
    c = ImmichClient("http://x", "k", max_retries=0, retry_backoff=0)

    class AlwaysFailSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, timeout=None, **kwargs):
            raise mm.module.requests.RequestException("fatal")

    c.session = AlwaysFailSession()
    with pytest.raises(mm.module.requests.RequestException):
        c._request("GET", "http://x/api/ping")


def test_request_unreachable_guard_path():
    c = ImmichClient("http://x", "k")
    c.max_retries = -1  # force attempts=0 to exercise the guard

    class NoopSession:
        def __init__(self):
            self.headers = {}

        def request(self, method, url, timeout=None, **kwargs):
            return DummyResp(status_code=200)

    c.session = NoopSession()
    with pytest.raises(RuntimeError):
        c._request("GET", "http://x/api/ping")


def test_search_metadata_400_error_logging_json_parse_failure():
    c = ImmichClient("http://x", "k")
    err = RuntimeError("boom")
    s = DummySession()
    s.post_responses = [DummyResp(status_code=400, payload=ValueError("json"), text="bad", raises=err)]
    c.session = s

    with pytest.raises(RuntimeError):
        c._search_metadata({"query": "x"})


def test_extract_to_int_to_asset_helpers():
    assert ImmichClient._extract_assets_page({"assets": {"items": [1], "nextPage": 2}}) == ([1], 2)
    assert ImmichClient._extract_assets_page({"assets": [1]}) == ([1], None)
    assert ImmichClient._extract_assets_page({"assets": "x"}) == ([], None)
    assert ImmichClient._to_int("4", 1) == 4
    assert ImmichClient._to_int("x", 9) == 9

    assert ImmichClient._to_asset({}) is None
    a = ImmichClient._to_asset(
        {
            "id": "a",
            "originalFileName": "p.jpg",
            "createdAt": "2024-01-01T00:00:00Z",
            "ownerId": "u",
            "assetType": "IMAGE",
            "isFavorite": True,
            "exifInfo": {"exifImageWidth": 4032, "exifImageHeight": 3024},
            "stack": {"id": "s1"},
        }
    )
    assert a is not None
    assert a.stackId == "s1"
    assert a.type == "image"
    assert a.isFavorite is True
    assert a.width == 4032
    assert a.height == 3024


def test_get_all_assets_pagination_and_filtering(monkeypatch):
    c = ImmichClient("http://x", "k")

    pages = [
        ({"assets": {"items": [{"id": "1", "fileCreatedAt": "2024-01-01T00:00:00Z", "ownerId": "u1", "type": "IMAGE"}], "nextPage": "2"}}, 200),
        ({"assets": {"items": [{"id": "2", "fileCreatedAt": "2024-01-01T00:00:01Z", "ownerId": "u2", "type": "IMAGE"}], "nextPage": 0}}, 200),
        ({"assets": []}, 200),
    ]

    def fake_search(payload):
        body, code = pages.pop(0)
        return DummyResp(status_code=code, payload=body)

    monkeypatch.setattr(c, "_search_metadata", fake_search)
    assets = c.get_all_assets(user_id="u1")
    assert [a.id for a in assets] == ["1"]


def test_get_current_user_id_success_and_failure():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=200, payload={"id": "u1"})]
    c.session = s
    assert c.get_current_user_id() == "u1"
    assert c.get_current_user_id() == "u1"

    c2 = ImmichClient("http://x", "k")
    s2 = DummySession()
    s2.get_responses = [DummyResp(status_code=500, raises=RuntimeError("x"))]
    c2.session = s2
    assert c2.get_current_user_id() is None


def test_get_asset_thumbnail_paths():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [
        DummyResp(status_code=404),
        DummyResp(status_code=200, content=make_image_bytes()),
    ]
    c.session = s
    im = c.get_asset_thumbnail("a")
    assert im is not None


def test_get_asset_thumbnail_video_preview_quick_skip():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=404), DummyResp(status_code=200, content=make_image_bytes())]
    c.session = s

    assert c.get_asset_thumbnail("v1", asset_type="video", skip_video_preview_404=True) is None
    # Preview 404 should short-circuit for video when enabled.
    assert len(s.get_calls) == 1


def test_get_asset_thumbnail_video_preview_no_quick_skip():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=404), DummyResp(status_code=200, content=make_image_bytes())]
    c.session = s

    im = c.get_asset_thumbnail("v1", asset_type="video", skip_video_preview_404=False)
    assert im is not None
    assert len(s.get_calls) == 2


def test_get_video_frame_from_playback_ffmpeg_unavailable(monkeypatch):
    c = ImmichClient("http://x", "k")
    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: None)
    image, reason = c.get_video_frame_from_playback("v1")
    assert image is None
    assert reason == "ffmpeg-unavailable"


def test_get_video_frame_from_playback_success(monkeypatch):
    c = ImmichClient("http://x", "k")

    class Proc:
        returncode = 0
        stdout = make_image_bytes()
        stderr = b""

    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("tests._module.module.subprocess.run", lambda *args, **kwargs: Proc())

    image, reason = c.get_video_frame_from_playback("v1")
    assert image is not None
    assert reason == "ffmpeg-frame"


def test_get_video_frame_from_playback_timeout(monkeypatch):
    c = ImmichClient("http://x", "k")
    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: "/usr/bin/ffmpeg")

    def raise_timeout(*_args, **_kwargs):
        raise mm.module.subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=1)

    monkeypatch.setattr("tests._module.module.subprocess.run", raise_timeout)
    image, reason = c.get_video_frame_from_playback("v1")
    assert image is None
    assert reason == "ffmpeg-timeout"


def test_get_video_frame_from_playback_generic_error(monkeypatch):
    c = ImmichClient("http://x", "k")
    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("tests._module.module.subprocess.run", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("x")))
    image, reason = c.get_video_frame_from_playback("v1")
    assert image is None
    assert reason == "ffmpeg-error"


def test_get_video_frame_from_playback_no_frame(monkeypatch):
    c = ImmichClient("http://x", "k")

    class Proc:
        returncode = 1
        stdout = b""
        stderr = b"err"

    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("tests._module.module.subprocess.run", lambda *args, **kwargs: Proc())
    image, reason = c.get_video_frame_from_playback("v1")
    assert image is None
    assert reason == "ffmpeg-no-frame"


def test_get_video_frame_from_playback_decode_error(monkeypatch):
    c = ImmichClient("http://x", "k")

    class Proc:
        returncode = 0
        stdout = b"not-an-image"
        stderr = b""

    monkeypatch.setattr("tests._module.module.shutil.which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr("tests._module.module.subprocess.run", lambda *args, **kwargs: Proc())
    image, reason = c.get_video_frame_from_playback("v1")
    assert image is None
    assert reason == "ffmpeg-decode-error"


def test_get_asset_thumbnail_permission_denied_once():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [DummyResp(status_code=403, payload={"message": "missing asset.view"})]
    c.session = s
    assert c.get_asset_thumbnail("a") is None

    s.get_responses = [DummyResp(status_code=403, payload={"message": "missing asset.view"})]
    assert c.get_asset_thumbnail("a") is None


def test_get_asset_thumbnail_raises_for_nonhandled_error():
    c = ImmichClient("http://x", "k")
    c.max_retries = 0
    s = DummySession()
    s.get_responses = [DummyResp(status_code=500, raises=RuntimeError("bad"))]
    c.session = s
    with pytest.raises(RuntimeError):
        c.get_asset_thumbnail("a")


def test_stack_crud_helpers_and_stack_parsing():
    c = ImmichClient("http://x", "k")
    c.max_retries = 0
    s = DummySession()
    s.post_responses = [DummyResp(status_code=201), DummyResp(status_code=400, text="bad")]
    s.delete_responses = [DummyResp(status_code=204), DummyResp(status_code=500, text="bad")]
    s.get_responses = [
        DummyResp(
            status_code=200,
            payload=[
                {
                    "id": "st1",
                    "primaryAssetId": "a1",
                    "assets": [{"id": "a2", "ownerId": "u2"}],
                },
                {
                    "id": None,
                    "primaryAssetId": "b1",
                    "assets": [{"id": "b1", "ownerId": "u3"}],
                },
            ],
        ),
        DummyResp(
            status_code=200,
            payload=[
                {
                    "id": "st1",
                    "primaryAssetId": "a1",
                    "assets": [{"id": "a2", "ownerId": "u2"}],
                }
            ],
        ),
    ]
    c.session = s

    assert c.create_stack("p", ["c1"]) is True
    assert c.create_stack("p", ["c1"]) is False
    assert c.create_stack("p", []) is False

    assert c.delete_stack("x") is True
    assert c.delete_stack("x") is False

    stacks = c.get_stacks()
    assert stacks[0]["assetIds"] == ["a2", "a1"]
    existing = c.get_existing_stacks()
    assert "st1" in existing


def test_stack_owner_fallback():
    payload = {"primaryAssetId": "x", "assets": [{"id": "y", "userId": "u9"}]}
    assert ImmichClient._stack_owner_id(payload) == "u9"
    assert ImmichClient._stack_owner_id({"assets": []}) is None


def test_stack_helpers_support_alternate_shapes():
    payload = {
        "id": "st1",
        "primaryAsset": {"id": "p1", "ownerId": "u1"},
        "assetIds": ["p1", "c1"],
        "secondaryAssetIds": ["c2"],
    }

    assert ImmichClient._stack_owner_id(payload) == "u1"
    assert ImmichClient._stack_asset_ids(payload) == ["p1", "c1", "c2"]


def test_get_stacks_primary_asset_object_shape():
    c = ImmichClient("http://x", "k")
    s = DummySession()
    s.get_responses = [
        DummyResp(
            status_code=200,
            payload=[
                {
                    "id": "st1",
                    "primaryAsset": {"id": "p1", "ownerId": "u1"},
                    "assetIds": ["p1", "c1"],
                }
            ],
        )
    ]
    c.session = s

    stacks = c.get_stacks()
    assert stacks[0]["primaryAssetId"] == "p1"
    assert stacks[0]["assetIds"] == ["p1", "c1"]
