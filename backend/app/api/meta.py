"""What this server is and can do, for clients and scripts (no sign-in needed)."""

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import API_VERSION, APP_VERSION
from app.db import get_db
from app.services import app_settings, radio_hat

router = APIRouter(prefix="/api", tags=["meta"])

# Everything every install of this version supports. Add a name when a feature is added, so an
# app can tell an older server apart from one that lacks the feature for another reason.
FEATURES = [
    "signed_in_devices",
    "backup",
    "remote_admin",
    "bot",
    "weather",
    "bot_apps",
    "firmware_check",
    "api_keys",
    "contact_import",
]
# Only on native (Debian / Raspberry Pi) installs, which have the root helper.
NATIVE_FEATURES = ["software_updates", "network_settings", "radio_hat", "system_backup"]


class Meta(BaseModel):
    product: Literal["meshhome"] = "meshhome"
    version: str
    api_version: int
    install_kind: Literal["native", "container"]
    needs_setup: bool
    features: list[str]


@router.get("/meta", response_model=Meta)
async def meta(db: AsyncSession = Depends(get_db)) -> Meta:
    native = radio_hat.native()
    return Meta(
        version=APP_VERSION,
        api_version=API_VERSION,
        install_kind="native" if native else "container",
        needs_setup=not await app_settings.setup_complete(db),
        features=FEATURES + (NATIVE_FEATURES if native else []),
    )
