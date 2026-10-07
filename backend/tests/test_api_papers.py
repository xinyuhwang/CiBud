from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from cibud.api.deps import get_session, get_store
from cibud.api.main import create_app
from cibud.storage import LocalObjectStore

PROFILE = {"problem": "p", "method": "m", "data": "d", "contribution": "c"}
PDF = b"%PDF-1.7 fake"


@pytest.fixture
def client(factory: sessionmaker[Session], tmp_path: Path) -> Iterator[TestClient]:
    app = create_app()

    def session() -> Iterator[Session]:
        with factory() as s, s.begin():
            yield s

    store = LocalObjectStore(tmp_path)
    app.dependency_overrides[get_session] = session
    app.dependency_overrides[get_store] = lambda: store
    yield TestClient(app)


def make_project(client: TestClient) -> str:
    resp = client.post("/projects", json={"name": "Sepsis", "profile": PROFILE})
    assert resp.status_code == 201
    project_id: str = resp.json()["id"]
    return project_id


def test_project_round_trip(client: TestClient) -> None:
    project_id = make_project(client)
    body = client.get(f"/projects/{project_id}").json()
    assert body["name"] == "Sepsis"
    assert body["profile"]["version"] == 1


def test_upload_flow(client: TestClient) -> None:
    project_id = make_project(client)
    resp = client.post(
        f"/projects/{project_id}/papers",
        files={"file": ("paper.pdf", PDF, "application/pdf")},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["created"] is True
    paper_id, job_id = body["paper"]["id"], body["job"]["id"]

    assert [p["id"] for p in client.get(f"/projects/{project_id}/papers").json()] == [paper_id]
    detail = client.get(f"/papers/{paper_id}").json()
    assert detail["paper"]["state"] == "imported"
    assert detail["reference"]["csl"]["title"] == "paper"
    assert client.get(f"/papers/{paper_id}/passages").json() == []
    assert client.get(f"/jobs/{job_id}").json()["status"] == "queued"

    again = client.post(
        f"/projects/{project_id}/papers",
        files={"file": ("copy.pdf", PDF, "application/pdf")},
    ).json()
    assert again["created"] is False
    assert again["paper"]["id"] == paper_id


def test_rejects_non_pdf(client: TestClient) -> None:
    project_id = make_project(client)
    resp = client.post(
        f"/projects/{project_id}/papers", files={"file": ("x.pdf", b"nope", "application/pdf")}
    )
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "path",
    ["/projects/proj_nope", "/projects/proj_nope/papers", "/papers/paper_nope", "/jobs/job_nope"],
)
def test_not_found(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404


def test_retry_requires_failed_job(client: TestClient) -> None:
    project_id = make_project(client)
    job_id = client.post(
        f"/projects/{project_id}/papers", files={"file": ("p.pdf", PDF, "application/pdf")}
    ).json()["job"]["id"]
    assert client.post(f"/jobs/{job_id}/retry").status_code == 409
