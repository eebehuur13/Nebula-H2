"""Offline tests for the Drive, Notion and Granola connectors (every HTTP call is mocked)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from nebula.config import settings
from nebula.connectors import GoogleDriveConnector, get_connector
from nebula.connectors import _http, gdrive
from nebula.connectors.base import Change

FOLDER = gdrive.FOLDER
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"


# ---------------------------------------------------------------------------
# Shared fixtures


@pytest.fixture(autouse=True)
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make backoff and rate limiting instant; record the delays asked for."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(_http, "sleep", fake_sleep)
    return delays


@pytest.fixture(autouse=True)
def credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NEBULA_NOTION_TOKEN", "secret-notion")
    monkeypatch.setenv("NEBULA_GRANOLA_API_KEY", "grn_secret")
    settings.cache_clear()
    yield
    settings.cache_clear()


class FakeCredentials:
    """Stands in for google-auth service-account credentials."""

    def __init__(self) -> None:
        self.token: str | None = None
        self.valid = False
        self.refreshes = 0
        self.service_account_email = "nebula@rl-nebula.iam.gserviceaccount.com"

    def refresh(self, request: Any) -> None:
        self.refreshes += 1
        self.token, self.valid = "test-token", True


@pytest.fixture
def fake_creds(monkeypatch: pytest.MonkeyPatch) -> FakeCredentials:
    creds = FakeCredentials()
    monkeypatch.setattr(gdrive, "load_credentials", lambda: creds)
    return creds


@pytest.fixture
def router():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def ok(data: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)


# ---------------------------------------------------------------------------
# Google Drive


class FakeDrive:
    """Minimal Drive v3: files.list keyed by `q` (pages of results), files.get, export, changes."""

    def __init__(self, router: respx.MockRouter):
        self.lists: dict[str, list[list[dict[str, Any]]]] = {}
        self.files: dict[str, dict[str, Any]] = {}
        self.content: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.auth_headers: set[str] = set()
        self.permissions: dict[str, list[dict[str, Any]] | None] = {}  # None = caller can't list them
        host = "www.googleapis.com"
        router.get(host=host, path="/drive/v3/files").mock(side_effect=self.list)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)$").mock(side_effect=self.get)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)/export$").mock(side_effect=self.export)
        router.get(host=host, path__regex=r"^/drive/v3/files/(?P<file_id>[^/]+)/permissions$").mock(
            side_effect=self.perms)

    def _seen(self, request: httpx.Request) -> None:
        self.auth_headers.add(request.headers.get("authorization", ""))

    def list(self, request: httpx.Request) -> httpx.Response:
        self._seen(request)
        params = request.url.params
        assert params["supportsAllDrives"] == "true" and params["includeItemsFromAllDrives"] == "true"
        pages = self.lists.get(params["q"], [[]])
        index = int(params.get("pageToken") or 0)
        body: dict[str, Any] = {"files": pages[index]}
        if index + 1 < len(pages):
            body["nextPageToken"] = str(index + 1)
        return ok(body)

    def get(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        if file_id not in self.files:
            return ok({"error": {"code": 404, "errors": [{"reason": "notFound"}]}}, 404)
        if request.url.params.get("alt") == "media":
            self.downloads.append(file_id)
            return httpx.Response(200, content=self.content[file_id])
        return ok(self.files[file_id])

    def perms(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        listed = self.permissions.get(file_id, [])
        if listed is None:
            return ok({"error": {"code": 403, "errors": [{"reason": "insufficientFilePermissions"}]}}, 403)
        return ok({"permissions": listed})

    def export(self, request: httpx.Request, file_id: str) -> httpx.Response:
        self._seen(request)
        self.downloads.append(file_id)
        assert request.url.params["mimeType"] == "text/markdown"
        return httpx.Response(200, content=self.content[file_id])


def drive_file(fid: str, name: str, mime: str, parent: str, **extra: Any) -> dict[str, Any]:
    return {"id": fid, "name": name, "mimeType": mime, "parents": [parent],
            "modifiedTime": "2026-10-01T10:00:00Z", "createdTime": "2026-09-01T10:00:00Z",
            "webViewLink": f"https://drive.google.com/{fid}", **extra}


FOLDERS_Q = f"mimeType = '{FOLDER}' and trashed = false"
FILES_Q = f"mimeType != '{FOLDER}' and trashed = false"


async def test_drive_full_listing_of_shared_drive(router, fake_creds):
    drive = FakeDrive(router)
    router.get("https://www.googleapis.com/drive/v3/drives/drv1").mock(return_value=ok({"id": "drv1", "name": "Programme"}))
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(return_value=ok({"startPageToken": "100"}))
    drive.lists[FOLDERS_Q] = [
        [drive_file("fA", "AP", FOLDER, "drv1"), drive_file("fB", "Reports", FOLDER, "fA")],
        [drive_file("fL", "HR", FOLDER, "drv1", inheritedPermissionsDisabled=True)],
    ]
    drive.lists[FILES_Q] = [
        [drive_file("d1", "Plan", DOC, "fB"), drive_file("s1", "Budget", SHEET, "fA"),
         drive_file("p1", "deck.pdf", "application/pdf", "drv1", size="1000")],
        [drive_file("x1", "salaries.docx", gdrive.DOCX, "fL", size="2000"),
         drive_file("v1", "clip.mp4", "video/mp4", "fA", size="10"),
         drive_file("big", "huge.pdf", "application/pdf", "fA", size=str(60 * 1024 * 1024)),
         drive_file("sc", "link", "application/vnd.google-apps.shortcut", "fA")],
    ]
    connector = GoogleDriveConnector({"roots": [{"id": "drv1", "kind": "drive"}]})

    first = await connector.list_changes(None)
    assert first.has_more
    by_id = {c.external_id: c for c in first.changes}
    assert set(by_id) == {"d1", "s1", "p1"}
    assert by_id["d1"].path == ["Programme", "AP", "Reports"]
    assert by_id["s1"].path == ["Programme", "AP"]
    assert by_id["p1"].path == ["Programme"]

    second = await connector.list_changes(first.next_cursor)
    assert not second.has_more
    assert [c.external_id for c in second.changes] == ["x1"]  # mp4, >50 MB pdf and shortcut skipped
    assert second.changes[0].path == ["Programme", "HR"]

    assert by_id["d1"].root == "drv1"
    state = json.loads(second.next_cursor)
    assert state["v"] == 3 and not state["full"] and state["token"] == "100" and state["drives"] == []
    assert state["roots"] == {"drv1": "drive"}
    assert set(state["folders"]) == {"drv1", "fA", "fB", "fL"}
    assert fake_creds.refreshes == 1 and drive.auth_headers == {"Bearer test-token"}
    await connector.close()


async def test_drive_incremental_changes_folder_root(router, fake_creds):
    drive = FakeDrive(router)
    cursor = json.dumps({
        "v": 3, "roots": {"root1": "folder"}, "token": "100", "queue": [], "pending": [], "drives": [], "full": False,
        "folders": {"root1": ["Root", "", "", ""], "fA": ["AP", "root1", "", ""],
                    "fB": ["Reports", "fA", "", ""]},
    })
    changes = [
        {"changeType": "file", "fileId": "d1", "removed": False, "file": drive_file("d1", "Plan", DOC, "fA")},
        {"changeType": "file", "fileId": "gone1", "removed": True},
        # moved out of the root: its new parent is a folder outside the collection
        {"changeType": "file", "fileId": "m1", "removed": False, "file": drive_file("m1", "Moved", DOC, "elsewhere")},
        # a whole folder moved out: everything below it goes
        {"changeType": "file", "fileId": "fB", "removed": False, "file": drive_file("fB", "Reports", FOLDER, "elsewhere")},
        {"changeType": "file", "fileId": "t1", "removed": False, "file": drive_file("t1", "Old", DOC, "fA", trashed=True)},
        # a new folder inside the root: its contents are listed
        {"changeType": "file", "fileId": "fN", "removed": False, "file": drive_file("fN", "New", FOLDER, "fA")},
    ]
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": changes, "newStartPageToken": "200"}))
    drive.files["elsewhere"] = {"id": "elsewhere", "name": "Someone's folder", "mimeType": FOLDER}
    drive.lists["'fB' in parents"] = [[drive_file("d2", "Q2", DOC, "fB")]]
    drive.lists["'fN' in parents and trashed = false"] = [[drive_file("n1", "note.pdf", "application/pdf", "fN", size="5")]]

    connector = GoogleDriveConnector({"roots": [{"id": "root1", "kind": "folder"}]})
    batch = await connector.list_changes(cursor)
    by_id = {c.external_id: c for c in batch.changes}

    assert not by_id["d1"].deleted and by_id["d1"].path == ["Root", "AP"]
    assert by_id["gone1"].deleted
    assert by_id["m1"].deleted
    assert by_id["d2"].deleted
    assert by_id["t1"].deleted
    assert not by_id["n1"].deleted and by_id["n1"].path == ["Root", "AP", "New"]
    assert not batch.has_more
    state = json.loads(batch.next_cursor)
    assert state["token"] == "200"
    assert "fB" not in state["folders"] and state["folders"]["fN"] == ["New", "fA", "", ""]
    await connector.close()


def drive_cursor(roots: dict[str, str], folders: dict[str, list[Any]], token: str = "100") -> str:
    return json.dumps({"v": 3, "roots": roots, "token": token, "queue": [], "pending": [], "drives": [],
                       "full": False, "folders": folders})


async def test_drive_many_roots_folder_file_and_new_root(router, fake_creds):
    """Folders and single files shared with Nebula are roots of one source; a root added later is walked
    on the next sync while the change feed covers the rest."""
    drive = FakeDrive(router)
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(return_value=ok({"startPageToken": "7"}))
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": [], "newStartPageToken": "8"}))
    drive.files["rootA"] = drive_file("rootA", "Team A", FOLDER, "someone", permissionIds=["p1"])
    drive.files["solo"] = drive_file("solo", "Budget note", DOC, "private", permissionIds=["p9", "p2"])
    drive.lists["'rootA' in parents and trashed = false"] = [[
        drive_file("a1", "Plan", DOC, "rootA", permissionIds=["p1"], md5Checksum="x"),
        drive_file("subB", "Sub", FOLDER, "rootA"),
    ]]
    drive.lists["'subB' in parents and trashed = false"] = [[drive_file("b1", "Notes", DOC, "subB")]]

    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "solo", "kind": "file"}]})
    batch = await connector.list_changes(None)
    by_id = {c.external_id: c for c in batch.changes}
    assert set(by_id) == {"a1", "b1", "solo"} and not batch.has_more and not batch.lost_roots
    assert by_id["solo"].root == "solo" and by_id["solo"].path == [] and by_id["solo"].acl == "p2,p9"
    assert by_id["b1"].root == "rootA" and by_id["b1"].path == ["Team A", "Sub"]
    assert by_id["a1"].rev == "2026-10-01T10:00:00Z|x" and by_id["a1"].acl == "p1"
    assert by_id["b1"].acl is None  # Drive didn't say: the worker falls back to reading sharing on full listings
    await connector.close()

    # A third root shared later: walked first, then the change feed in the same sync.
    drive.files["rootC"] = drive_file("rootC", "Team C", FOLDER, "elsewhere")
    drive.lists["'rootC' in parents and trashed = false"] = [[drive_file("c1", "Minutes", DOC, "rootC")]]
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "solo", "kind": "file"},
                                                {"id": "rootC", "kind": "folder"}]})
    walked = await connector.list_changes(batch.next_cursor)
    assert [c.external_id for c in walked.changes] == ["c1"] and walked.has_more
    feed = await connector.list_changes(walked.next_cursor)
    assert feed.changes == [] and not feed.has_more and json.loads(feed.next_cursor)["token"] == "8"
    await connector.close()

    # A root that's gone from the config is forgotten; one Nebula can't open is reported as lost.
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "ghost", "kind": "folder"}]})
    later = await connector.list_changes(feed.next_cursor)
    assert later.lost_roots == ["ghost"]
    assert set(json.loads(later.next_cursor)["roots"]) == {"rootA"}
    await connector.close()


async def test_drive_root_unshared_and_folder_resharing(router, fake_creds):
    drive = FakeDrive(router)
    cursor = drive_cursor({"rootA": "folder", "rootB": "folder"}, {
        "rootA": ["A", "", "", "p1"], "fX": ["X", "rootA", "", "p1"],
        "rootB": ["B", "", "", "p5"],
    })
    changes = [
        # Someone shared folder X with another person: everything below it gets its sharing re-read.
        {"changeType": "file", "fileId": "fX", "removed": False,
         "file": drive_file("fX", "X", FOLDER, "rootA", permissionIds=["p1", "p7"])},
        # Root B was unshared from Nebula.
        {"changeType": "file", "fileId": "rootB", "removed": True},
    ]
    router.get("https://www.googleapis.com/drive/v3/changes").mock(
        return_value=ok({"changes": changes, "newStartPageToken": "101"}))
    drive.lists["'fX' in parents and trashed = false"] = [[drive_file("x1", "Doc", DOC, "fX", permissionIds=["p1"])]]
    drive.lists["'rootB' in parents"] = [[drive_file("b1", "Old", DOC, "rootB")]]
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}, {"id": "rootB", "kind": "folder"}]})
    batch = await connector.list_changes(cursor)
    by_id = {c.external_id: c for c in batch.changes}
    assert by_id["x1"].recheck_acl and not by_id["x1"].deleted
    assert by_id["b1"].deleted and batch.lost_roots == ["rootB"]
    await connector.close()


async def test_drive_one_file_and_discovery(router, fake_creds):
    drive = FakeDrive(router)
    cursor = drive_cursor({"rootA": "folder"}, {"rootA": ["A", "", "", ""], "fX": ["X", "rootA", "", ""]})
    drive.files["x1"] = drive_file("x1", "Doc", DOC, "fX", permissionIds=["p1"])
    drive.files["lone"] = drive_file("lone", "Lone", DOC, "nowhere")
    connector = GoogleDriveConnector({"roots": [{"id": "rootA", "kind": "folder"}]})
    change = await connector.change_for("x1", cursor)
    assert change is not None and change.root == "rootA" and change.path == ["A", "X"]
    assert await connector.change_for("lone", cursor) is None  # readable, but not under anything shared
    assert await connector.change_for("missing", cursor) is None
    assert await connector.covering_root({"id": "fX", "parents": ["rootA"]}, cursor) == "rootA"

    drive.lists["sharedWithMe and trashed = false"] = [[
        drive_file("fold", "Shared folder", FOLDER, "x", sharingUser={"emailAddress": "Priya@example.org"},
                   capabilities={"canEdit": True}),
        drive_file("doc", "Shared doc", DOC, "x", sharingUser={"emailAddress": "asha@example.org"},
                   capabilities={"canEdit": False}),
        drive_file("pic", "photo.jpg", "image/jpeg", "x", size="10"),
    ]]
    router.get("https://www.googleapis.com/drive/v3/drives").mock(
        return_value=ok({"drives": [{"id": "drv9", "name": "Programme"}]}))
    found = await connector.shared_with_nebula()
    assert found == [
        {"id": "fold", "name": "Shared folder", "kind": "folder", "shared_by": "priya@example.org", "editor": True},
        {"id": "doc", "name": "Shared doc", "kind": "file", "shared_by": "asha@example.org", "editor": False},
        {"id": "drv9", "name": "Programme", "kind": "drive", "shared_by": "", "editor": None},
    ]
    await connector.close()


def test_drive_links():
    fid = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    for link in (f"https://drive.google.com/drive/folders/{fid}", f"https://drive.google.com/drive/u/0/folders/{fid}?usp=x",
                 f"https://docs.google.com/document/d/{fid}/edit", f"https://drive.google.com/file/d/{fid}/view",
                 f"https://drive.google.com/open?id={fid}", fid):
        assert gdrive.drive_id_from(link) == fid
    with pytest.raises(ValueError):
        gdrive.drive_id_from("https://example.com/nope")


async def test_drive_fetch_exports_doc_as_markdown(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["d1"] = drive_file("d1", "Plan", DOC, "fA", owners=[{"emailAddress": "asha@example.org"}], version="7")
    drive.content["d1"] = b"# Plan\n\nHello"
    connector = GoogleDriveConnector({"roots": [{"id": "drv1", "kind": "drive"}]})

    item = await connector.fetch(Change(external_id="d1", path=["Programme", "AP"]))
    assert item is not None
    assert item.filename == "Plan.md" and item.mime_type == "text/markdown" and item.kind == "doc"
    assert item.data == b"# Plan\n\nHello"
    assert item.url == "https://drive.google.com/d1" and item.author == "asha@example.org"
    assert item.path == ["Programme", "AP"] and item.extra["version"] == "7"
    assert item.extra["audience"] == {"emails": [], "groups": [], "domains": [], "public": False, "unknown": False}
    await connector.close()


async def test_drive_fetch_reports_file_sharing(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["d1"] = drive_file("d1", "Story", "text/plain", "fA", size="5")
    drive.content["d1"] = b"hello"
    drive.permissions["d1"] = [
        {"type": "user", "role": "writer", "emailAddress": "Priya@Example.org"},
        {"type": "user", "role": "reader", "emailAddress": "friend@gmail.com"},
        {"type": "group", "role": "reader", "emailAddress": "ap-team@example.org"},
        {"type": "domain", "role": "reader", "domain": "example.org"},
        {"type": "anyone", "role": "reader"},
        {"type": "user", "role": "reader", "emailAddress": "gone@x.org", "deleted": True},
    ]
    drive.files["d2"] = drive_file("d2", "Locked", "text/plain", "fA", size="5")
    drive.content["d2"] = b"hello"
    drive.permissions["d2"] = None
    connector = GoogleDriveConnector({"roots": [{"id": "drv1", "kind": "drive"}]})

    item = await connector.fetch(Change(external_id="d1"))
    assert item.extra["audience"] == {
        "emails": ["friend@gmail.com", "priya@example.org"], "groups": ["ap-team@example.org"],
        "domains": ["example.org"], "public": True, "unknown": False}
    locked = await connector.fetch(Change(external_id="d2"))
    assert locked.extra["audience"]["unknown"] is True
    await connector.close()


async def test_drive_fetch_skips_unsupported_types(router, fake_creds):
    drive = FakeDrive(router)
    drive.files["v1"] = drive_file("v1", "clip.mp4", "video/mp4", "fA", size="10")
    drive.files["f1"] = drive_file("f1", "Survey", "application/vnd.google-apps.form", "fA")
    connector = GoogleDriveConnector({"roots": [{"id": "drv1", "kind": "drive"}]})

    assert await connector.fetch(Change(external_id="v1")) is None
    assert await connector.fetch(Change(external_id="f1")) is None
    assert await connector.fetch(Change(external_id="missing")) is None
    assert drive.downloads == []
    await connector.close()


def test_drive_classify():
    assert gdrive.classify({"name": "a", "mimeType": SHEET}) == gdrive.Plan("sheet", ".xlsx", gdrive.XLSX)
    assert gdrive.classify({"name": "notes.TXT", "mimeType": "text/plain", "size": "3"}).kind == "file"
    assert gdrive.classify({"name": "report", "mimeType": "application/pdf", "size": "3"}).ext == ".pdf"
    assert gdrive.classify({"name": "data.csv", "mimeType": "text/csv"}).kind == "sheet"
    assert gdrive.classify({"name": "x.zip", "mimeType": "application/zip"}) is None
    assert gdrive.classify({"name": "f", "mimeType": FOLDER}) is None


async def test_drive_backs_off_on_rate_limit(router, fake_creds, sleeps):
    rate_limited = ok({"error": {"code": 403, "errors": [{"reason": "userRateLimitExceeded"}]}}, 403)
    router.get("https://www.googleapis.com/drive/v3/changes/startPageToken").mock(
        side_effect=[rate_limited, ok({}, 429), ok({"startPageToken": "9"})])
    connector = GoogleDriveConnector({"roots": [{"id": "drv1", "kind": "drive"}]})
    assert await connector._start_token("drv1") == "9"
    assert len(sleeps) == 2 and all(d > 0 for d in sleeps)
    await connector.close()


# ---------------------------------------------------------------------------
# Registry


def test_registry():
    assert isinstance(get_connector("gdrive", {"roots": []}), GoogleDriveConnector)
    with pytest.raises(ValueError):
        get_connector("notion", {})
    with pytest.raises(ValueError):
        get_connector("gdrive", {})
