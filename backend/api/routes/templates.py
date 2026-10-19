"""Importing the authored spreadsheet, and reading back what it became."""

from __future__ import annotations

import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, File, Query, UploadFile, status
from openpyxl.utils.exceptions import InvalidFileException

from api.deps import SessionDep
from api.schemas import TemplateEntryOut, TemplateImportOut, TemplateOut, TemplateSummaryOut
from domains.strength import models, services
from domains.strength.importer import TemplateImportError, import_template

router = APIRouter(prefix="/templates", tags=["templates"])


@router.post("/import", response_model=TemplateImportOut, status_code=status.HTTP_201_CREATED)
def import_xlsx(
    session: SessionDep,
    file: UploadFile = File(description="The authored .xlsx template"),
    name: str = Query(default="B1", description="Template name"),
    replace: bool = Query(
        default=False,
        description="Replace the entries of an existing template of this name",
    ),
) -> TemplateImportOut:
    """Import a template from an uploaded workbook.

    The upload is written to a temp **directory** under its original filename
    rather than to a `NamedTemporaryFile`: `parse_workbook` reads `path.name`
    into `block_template.source_file`, so a temp name would record
    `tmpw7f2k1.xlsx` as the provenance of every weight in the block.

    `replace` is not in spec 06's signature. It is here because the importer
    already supports it and spec 17's wizard offers it, and because the
    alternative -- a 409 with no way past it -- makes a corrected spreadsheet
    unimportable.
    """
    source = Path(file.filename or "upload.xlsx").name
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / source
        with path.open("wb") as handle:
            shutil.copyfileobj(file.file, handle)
        try:
            template_id = import_template(session, path, name=name, replace=replace)
        except (zipfile.BadZipFile, InvalidFileException) as exc:
            # An `.xlsx` is a zip, and openpyxl raises these before the
            # importer ever sees a cell. Translated here rather than in
            # `importer.py` because only an upload can be junk: the CLI is
            # handed a path that already exists, and spec 03's error is about
            # a workbook's *contents*.
            raise TemplateImportError(f"{source} is not a readable .xlsx file: {exc}") from exc

    session.commit()
    entries = services.template_entries(session, template_id)
    return TemplateImportOut(template_id=template_id, entries=len(entries))


@router.get("", response_model=list[TemplateSummaryOut])
def list_templates(session: SessionDep) -> list[TemplateSummaryOut]:
    """Every imported template, newest first, without their rows.

    Spec 17's first step has to show an existing template, and until now there
    was no way to learn a template's id without already knowing it. The entry
    count is a grouped `COUNT` rather than `len(template_entries(...))` so
    listing does not load 77 rows per template just to throw them away.
    """
    counts = {
        template_id: count
        for template_id, count in session.execute(
            sa.select(models.TemplateEntry.template_id, sa.func.count()).group_by(
                models.TemplateEntry.template_id
            )
        )
    }
    templates = session.scalars(
        sa.select(models.BlockTemplate).order_by(models.BlockTemplate.created_at.desc())
    )
    return [
        TemplateSummaryOut(
            id=template.id,
            name=template.name,
            weeks=template.weeks,
            slots=template.slots,
            source_file=template.source_file,
            entries=counts.get(template.id, 0),
        )
        for template in templates
    ]


@router.get("/{template_id}", response_model=TemplateOut)
def read_template(template_id: uuid.UUID, session: SessionDep) -> TemplateOut:
    template = services.get_template(session, template_id)
    return TemplateOut(
        id=template.id,
        name=template.name,
        weeks=template.weeks,
        slots=template.slots,
        overreach_from_week=template.overreach_from_week,
        source_file=template.source_file,
        entries=[
            TemplateEntryOut.of(entry) for entry in services.template_entries(session, template_id)
        ],
    )
