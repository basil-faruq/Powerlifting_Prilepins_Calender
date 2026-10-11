"""Shared FastAPI dependencies. Routes stay thin; these are what they lean on."""

from typing import Annotated

from fastapi import Depends
from sqlmodel import Session

from planner_core.config import Settings, get_settings
from planner_core.db import get_session

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
