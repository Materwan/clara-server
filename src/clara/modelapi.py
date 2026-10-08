"""The routes of the models (models.py): what a person may choose and what they chose, and the administrators'
catalogue. Like projects, a person is asked for with an account (`surface`, `user_id`); their choices are kept per
person and surface, so the same on every computer where they log in.

    GET    /v1/models                   ?surface=&user_id=  the models the person may choose (with what a token of each
                                        costs, in credits), what they chose for each surface, and the model they
                                        are answered by on this one
    PUT    /v1/models/choice            {surface, user_id, model, for_surface?}: choose a model for a surface
                                        (`for_surface`, default `surface`); null: the server's own. Not Discord's:
                                        an administrator sets that one

    GET    /v1/admin/catalog            ?refresh=true  every model of every provider that can be used, which ones
                                        users may choose, the weights, the model of Discord
    PATCH  /v1/admin/catalog            {refs, enabled?, weight?, auto_weight?}: select or unselect models, set a
                                        weight (credits a token) or go back to the one worked out from the size
    PUT    /v1/admin/catalog/discord    {model}: the model the Discord surface answers with (null: the server's own)
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth import Admin, Client, require_account
from .models import DISCORD, MAX_WEIGHT, MIN_WEIGHT, ModelCatalog, ModelInfo
from .providers import ProviderError, make_ref

log = logging.getLogger("clara")

router = APIRouter()

Surface = Annotated[str, Query(pattern=r"^[a-z0-9_-]{1,32}$")]
ExternalId = Annotated[str, Query(min_length=1, max_length=128)]
SURFACE = r"^[a-z0-9_-]{1,32}$"


class ChoiceBody(BaseModel):
    surface: str = Field(pattern=SURFACE)
    user_id: str = Field(min_length=1, max_length=128)
    model: str | None = Field(max_length=300)  # provider:model; null: the server's own
    for_surface: str | None = Field(default=None, pattern=SURFACE)


class CatalogPatch(BaseModel):
    refs: list[str] = Field(min_length=1, max_length=2000)
    enabled: bool | None = None
    weight: float | None = Field(default=None, ge=MIN_WEIGHT, le=MAX_WEIGHT)
    auto_weight: bool = False  # back to the weight worked out from the size


class DiscordModel(BaseModel):
    model: str | None = Field(max_length=300)


def _label(request: Request, info: ModelInfo) -> dict:
    catalog: ModelCatalog = request.app.state.models
    return info.describe(catalog.provider_label(info.provider))


def _current(request: Request, person_id: int | None, surface: str) -> dict:
    """The model a person is answered by on a surface (`own_key`: on their own key, so it costs no credits)."""
    catalog: ModelCatalog = request.app.state.models
    chosen = catalog.choose(surface, person_id)
    described = _label(request, catalog.info(chosen.ref))
    return {**described, "weight": chosen.weight, "own_key": chosen.own_key}


def _default(request: Request) -> dict:
    state = request.app.state
    providers, catalog = state.providers, state.models
    ref = providers.default_ref
    info = catalog.info(ref)
    return {**_label(request, info), "enabled": info.enabled}


async def _personal(request: Request, person_id: int) -> list[dict]:
    """The models of the providers this person brought a key for: all the provider offers to that key, costing
    no credits (userkeys.py)."""
    state = request.app.state
    keys, catalog = state.user_keys, state.models
    found = []
    for provider in keys.saved(person_id):
        for name in await keys.models(person_id, provider):
            ref = make_ref(provider, name)
            found.append({
                "ref": ref, "provider": provider, "provider_label": catalog.provider_label(provider), "name": name,
                "weight": 0, "own_key": True,
            })
    return found


@router.get("/v1/models")
async def models_of_person(client: Client, request: Request, surface: Surface, user_id: ExternalId) -> dict:
    require_account(request, client, surface, user_id)
    state = request.app.state
    catalog: ModelCatalog = state.models
    person = state.memory.find_person(surface, user_id)
    return {
        "surface": surface,
        "models": [_label(request, info) for info in catalog.usable()],
        "personal": await _personal(request, person.id) if person and surface != DISCORD else [],
        "default": _default(request),
        "choices": catalog.choices_of(person.id) if person else {},
        "current": _current(request, person.id if person else None, surface),
        "chosen_by_admin": surface == DISCORD,  # the model of this surface is not the person's to choose
        "surfaces": sorted(state.settings.user_surfaces),
    }


@router.put("/v1/models/choice")
async def choose_model(body: ChoiceBody, client: Client, request: Request) -> dict:
    require_account(request, client, body.surface, body.user_id)
    state = request.app.state
    catalog: ModelCatalog = state.models
    target = body.for_surface or body.surface
    if target == DISCORD:
        raise HTTPException(403, "The model of Discord is chosen by an administrator.")
    person = state.memory.resolve(body.surface, body.user_id, None)
    try:
        catalog.set_choice(person.id, target, body.model)
    except ProviderError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s chose %s for %s", client, body.model or "the server's model", target)
    return {
        "choices": catalog.choices_of(person.id),
        "current": _current(request, person.id, body.surface),
    }


def _catalog_state(request: Request, found: list[ModelInfo]) -> dict:
    state = request.app.state
    catalog: ModelCatalog = state.models
    providers = state.providers
    return {
        "models": [_label(request, info) for info in found],
        "problems": catalog.problems,
        "default": providers.default_ref,
        "discord": catalog.discord_model(),
        "reference_b": catalog.reference,
        "providers": [{"id": c.id, "label": c.label, "usable": c.usable} for c in providers.configs.values()],
    }


@router.get("/v1/admin/catalog")
async def admin_catalog(admin: Admin, request: Request, refresh: bool = False) -> dict:
    catalog: ModelCatalog = request.app.state.models
    return _catalog_state(request, await catalog.listing(force=refresh))


@router.patch("/v1/admin/catalog")
async def admin_catalog_patch(body: CatalogPatch, admin: Admin, request: Request) -> dict:
    catalog: ModelCatalog = request.app.state.models
    if body.weight is not None and body.auto_weight:
        raise HTTPException(422, "Give weight or auto_weight, not both")
    if body.weight is not None and len(body.refs) != 1:
        raise HTTPException(422, "A weight is set for one model at a time")
    try:
        if body.enabled is not None:
            catalog.set_enabled(body.refs, body.enabled)
        if body.weight is not None or body.auto_weight:
            for ref in body.refs:
                catalog.set_weight(ref, body.weight)
    except ProviderError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s edited the models: %s", admin, ", ".join(body.refs[:5]) + (" ..." if len(body.refs) > 5 else ""))
    return _catalog_state(request, await catalog.listing())


@router.put("/v1/admin/catalog/discord")
async def admin_discord_model(body: DiscordModel, admin: Admin, request: Request) -> dict:
    catalog: ModelCatalog = request.app.state.models
    try:
        catalog.set_discord_model(body.model)
    except ProviderError as error:
        raise HTTPException(422, str(error)) from None
    log.info("%s set the model of Discord to %s", admin, body.model or "the server's own")
    return {"discord": catalog.discord_model(), "default": request.app.state.providers.default_ref}


def install(app: FastAPI) -> None:
    app.include_router(router)

