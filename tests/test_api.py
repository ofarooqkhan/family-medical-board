import io
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def make_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (100, 50), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(accounts, monkeypatch):
    from fastapi.testclient import TestClient
    import api
    from extraction import LabReportDocument
    from providers import FakeProvider
    from tests.test_board import RESPONSES
    from tests.test_documents import LAB
    api.PROVIDER_OVERRIDE = FakeProvider(responses={LabReportDocument: LAB, **RESPONSES})
    yield TestClient(api.app)
    api.PROVIDER_OVERRIDE = None


def _token(client, email):
    r = client.post("/auth/login", json={"email": email, "password": "correct horse battery"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_and_token_rules(client):
    assert client.get("/health").json() == {"ok": True}
    assert client.get("/patients").status_code == 401
    assert client.post("/auth/login", json={"email": "admin@example.com", "password": "nope nope nope"}).status_code == 401
    assert client.get("/patients", headers={"Authorization": "Bearer garbage"}).status_code == 401
    h = _token(client, "admin@example.com")
    assert client.get("/me", headers=h).json()["role"] == "admin"


def test_patient_document_review_medication_board_brief_flow(client):
    ed = _token(client, "editor@example.com")
    vw = _token(client, "viewer@example.com")
    r = client.post("/patients", json={"name": "Parent One", "age": 80}, headers=ed)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/patients", json={"name": "X"}, headers=vw).status_code == 403
    assert client.get(f"/patients/{pid}", headers=vw).json()["name"] == "Parent One"

    r = client.post(f"/patients/{pid}/documents", files={"file": ("lab.png", make_png(), "image/png")},
                    data={"kind": "lab_report"}, headers=ed)
    assert r.status_code == 201, r.text
    fid = r.json()["file_id"]
    assert r.json()["items"] == 2
    assert client.get(f"/patients/{pid}/documents/{fid}/pages/1.png", headers=vw).headers["content-type"] == "image/png"
    assert client.get(f"/patients/{pid}/documents/{fid}/pages/9.png", headers=vw).status_code == 404

    items = client.get(f"/patients/{pid}/review", headers=vw).json()
    hba1c = next(i for i in items if i["payload"]["test_name"] == "HbA1c")
    assert client.post(f"/patients/{pid}/review/{hba1c['id']}/confirm", json={"payload": hba1c["payload"]}, headers=vw).status_code == 403
    r = client.post(f"/patients/{pid}/review/{hba1c['id']}/confirm", json={"payload": hba1c["payload"]}, headers=ed)
    assert r.status_code == 200 and r.json()["vital_id"]
    assert client.get(f"/patients/{pid}/vitals", headers=vw).json()[0]["reading_type"] == "lab_hba1c"

    r = client.post(f"/patients/{pid}/medications", json={"name": "Metformin", "dose": "500 mg", "start_date": "2026-09-01"}, headers=ed)
    assert r.status_code == 201
    assert client.get(f"/patients/{pid}/medications", headers=vw).json()[0]["name"] == "Metformin"
    assert len(client.get(f"/patients/{pid}/medications/history", headers=vw).json()) == 1

    r = client.post(f"/patients/{pid}/appointments", json={"title": "Follow-up", "starts_at": "2027-01-05T10:00:00", "specialty": "Cardiology"}, headers=ed)
    assert r.status_code == 201
    ics = client.get(f"/patients/{pid}/appointments.ics", headers=vw).text
    assert "BEGIN:VEVENT" in ics and "Follow-up" in ics

    r = client.post(f"/patients/{pid}/board", json={"query": "How has his blood pressure changed?"}, headers=vw)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "done" and "Questions to ask" in r.json()["markdown"]
    assert len(client.get(f"/patients/{pid}/board", headers=vw).json()) == 1

    assert client.get(f"/patients/{pid}/brief", headers=vw).json()["patient"]["name"] == "Parent One"
    assert "Pre-visit brief" in client.get(f"/patients/{pid}/brief?format=md", headers=vw).text
    assert client.get(f"/patients/{pid}/brief?format=pdf", headers=vw).content[:5] == b"%PDF-"

    assert client.get("/audit", headers=vw).status_code == 403
    admin = _token(client, "admin@example.com")
    assert any(a["action"] == "board.run" for a in client.get(f"/audit?patient_id={pid}", headers=admin).json())
