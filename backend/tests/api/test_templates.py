"""Importing the authored workbook over HTTP.

These need `data/B1_Mine.xlsx`, which is gitignored, so they skip on a fresh
clone -- the same bargain the importer suite strikes. The end-to-end flow test
deliberately does not depend on them.
"""

from __future__ import annotations

import uuid

from tests.api.conftest import ALL_LIFTS


def _upload(api_client, path, **params):
    with path.open("rb") as handle:
        return api_client.post(
            "/templates/import",
            files={
                "file": (
                    path.name,
                    handle,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            params=params,
        )


def test_the_workbook_imports_as_seventy_seven_entries(api_client, workbook_path):
    response = _upload(api_client, workbook_path, name="B1-upload")
    assert response.status_code == 201, response.text
    assert response.json()["entries"] == 77

    template = api_client.get(f"/templates/{response.json()['template_id']}").json()
    assert template["weeks"] == 11
    assert template["slots"] == 3
    assert len(template["entries"]) == 77
    assert sorted({entry["lift_code"] for entry in template["entries"]}) == ALL_LIFTS


def test_the_upload_records_the_real_filename_as_provenance(api_client, workbook_path):
    """Not a temp name: `source_file` is where every weight in the block came from."""
    response = _upload(api_client, workbook_path, name="B1-provenance")
    template = api_client.get(f"/templates/{response.json()['template_id']}").json()
    assert template["source_file"] == workbook_path.name


def test_the_authored_intensities_survive_unreduced(api_client, workbook_path):
    """ADR-0003 rule 5: the athlete is shown the `70/120` they wrote."""
    response = _upload(api_client, workbook_path, name="B1-fractions")
    entries = api_client.get(f"/templates/{response.json()['template_id']}").json()["entries"]
    pairs = {(entry["pct_num"], entry["pct_den"]) for entry in entries}
    # A reduced pair would have collapsed the denominators to 100.
    assert any(float(den) != 100 for _, den in pairs)


def test_a_second_import_of_the_same_name_is_a_409(api_client, workbook_path):
    assert _upload(api_client, workbook_path, name="B1-twice").status_code == 201
    response = _upload(api_client, workbook_path, name="B1-twice")
    assert response.status_code == 409
    assert "B1-twice" in response.json()["detail"]


def test_replace_swaps_the_entries_and_keeps_the_template_id(api_client, workbook_path):
    first = _upload(api_client, workbook_path, name="B1-replace")
    assert first.status_code == 201

    again = _upload(api_client, workbook_path, name="B1-replace", replace="true")
    assert again.status_code == 201, again.text
    assert again.json()["template_id"] == first.json()["template_id"]
    assert again.json()["entries"] == 77


def test_a_file_that_is_not_a_workbook_is_a_400(api_client):
    response = api_client.post(
        "/templates/import",
        files={"file": ("notes.xlsx", b"this is not a zip archive", "application/vnd.ms-excel")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]


def test_an_unknown_template_is_a_404(api_client):
    assert api_client.get(f"/templates/{uuid.uuid4()}").status_code == 404


# --- listing --------------------------------------------------------------


def test_listing_templates_on_a_fresh_database_is_empty(api_client):
    response = api_client.get("/templates")
    assert response.status_code == 200
    assert response.json() == []


def test_a_listed_template_is_counted_but_carries_no_rows(api_client, template):
    """The wizard needs the shape, not 77 rows it will fetch by id if it must."""
    listed = api_client.get("/templates").json()
    assert len(listed) == 1
    row = listed[0]
    assert row["id"] == str(template.id)
    assert row["entries"] == 77
    assert (row["weeks"], row["slots"]) == (11, 3)
    assert "entries" in row and not isinstance(row["entries"], list)


def test_templates_are_listed_newest_first(api_client, db_session, template):
    import datetime as dt

    from domains.strength import models

    newer = models.BlockTemplate(name="B2-newer")
    newer.created_at = template.created_at + dt.timedelta(seconds=1)
    db_session.add(newer)
    db_session.flush()

    listed = api_client.get("/templates").json()
    assert [row["name"] for row in listed] == ["B2-newer", template.name]
    # A template with no rows is a zero, not a missing key.
    assert listed[0]["entries"] == 0
