import json
from .conftest import login

def approve(api, headers):
    preview = api.get("/api/v1/migrations/relationships", headers=headers).json()
    return api.post("/api/v1/migrations/relationships/reconcile", headers=headers,
                    json={"expected_snapshot":preview["snapshot_sha256"]})



def upload(api, headers, kind, csv_text, mapping):
    data = csv_text.encode("utf-8")
    preview = api.post("/api/v1/migrations/preview", headers=headers,
                       data={"kind": kind}, files={"file": ("data.csv", data)})
    assert preview.status_code == 200, preview.text
    spec = {"kind": kind, "source": "pipedrive", "digest": preview.json()["digest"],
            "columns": mapping, "strategy": "upsert"}
    result = api.post("/api/v1/migrations/commit", headers=headers,
                      data={"spec": json.dumps(spec)}, files={"file": ("data.csv", data)})
    assert result.status_code == 200, result.text
    return result.json()


def test_relationship_reconciliation_is_tenant_scoped_and_idempotent(api):
    a, b = login(api), login(api, "admin-b")
    name = {"external_id": "ID", "name": "Name"}
    with_company = {**name, "company_external_id": "Company ID"}
    with_both = {**with_company, "contact_external_id": "Contact ID"}

    # Import out of order; references remain unresolved, never guessed.
    upload(api, a, "deals", "ID,Name,Company ID,Contact ID\nD1,Deal,O1,P1\n", with_both)
    before = api.get("/api/v1/migrations/relationships", headers=a).json()
    assert before["unresolved_count"] == 2
    assert approve(api, b).json()["links_created"] == 0

    upload(api, a, "companies", "ID,Name\nO1,Acme\n", name)
    upload(api, a, "contacts", "ID,Name,Company ID\nP1,Alice,O1\n", with_company)
    result = approve(api, a)
    assert result.status_code == 200, result.text
    assert result.json()["resolved_references"] == 3
    assert result.json()["links_created"] == 3
    again = approve(api, a).json()
    assert again["links_created"] == 0 and again["links_updated"] == 0
    deal = api.get("/api/v1/records?kind=deals", headers=a).json()[0]["id"]
    links = api.get(f"/api/v1/records/{deal}/links", headers=a).json()
    assert {x["relation"] for x in links} == {"company", "contact"}
    assert api.get(f"/api/v1/records/{deal}/links", headers=b).status_code == 404

    # Same external identifier in another company must not resolve A's links.
    upload(api, b, "companies", "ID,Name\nO1,Other company\n", name)
    assert approve(api, b).json()["links_created"] == 0

    # Activities resolve to their opportunity (deal), contact and company.
    activity_mapping = {**with_both, "deal_external_id": "Deal ID"}
    upload(api, a, "activities", "ID,Name,Company ID,Contact ID,Deal ID\nA1,Call,O1,P1,D1\n", activity_mapping)
    activity_result = approve(api, a)
    assert activity_result.status_code == 200, activity_result.text
    assert activity_result.json()["links_created"] == 3
    activity_id = api.get("/api/v1/records?kind=activities", headers=a).json()[0]["id"]
    activity_links = api.get(f"/api/v1/records/{activity_id}/links", headers=a).json()
    assert {x["relation"] for x in activity_links} == {"company", "contact", "deal"}

    # A provider reference removed by upsert invalidates the corresponding link.
    upload(api, a, "deals", "ID,Name,Company ID,Contact ID\nD1,Deal changed,O1,\n", with_both)
    updated = approve(api, a).json()
    assert updated["links_removed"] == 1
    assert [x["relation"] for x in api.get(f"/api/v1/records/{deal}/links", headers=a).json()] == ["company"]
