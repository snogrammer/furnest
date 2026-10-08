"""``POST /discord/interactions``: where Discord delivers button clicks and modal submits.

Discord, not a browser, calls this route, so it sits outside dashboard auth and
CSRF. Its own checks, in order:

1. **Configured?** Without ``DISCORD_PUBLIC_KEY`` nothing can be verified and the
   route answers 404, as if it didn't exist.
2. **Signed and fresh?** A missing or wrong Ed25519 signature, or a timestamp more
   than ``DISCORD_INTERACTION_MAX_AGE_SECONDS`` off, gets a 401. Discord itself sends
   badly signed requests when the endpoint is registered and expects exactly that.
3. **PING?** Answered with PONG, without touching the database.
4. **Allowed?** A signature only proves Discord sent the click. Clicks from outside
   ``DISCORD_CHANNEL_ID``, or, when ``DISCORD_ALLOWED_USER_IDS`` is set, from anyone
   not on it, get an ephemeral refusal and change nothing.
5. **Seen before?** The interaction id is recorded in ``discord_interactions`` in the
   same transaction as the dispatcher's work. A repeat gets a deferred-update ack and
   does nothing.

The checks are in :mod:`furnest.notify.interactions` and the dispatch in
:mod:`furnest.web.discord_dispatch`. Database work runs in the threadpool, so a
slow SQLite lock can't stall the event loop.
"""

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from furnest.config import Settings
from furnest.db.base import utcnow
from furnest.db.discord_store import record_interaction
from furnest.db.session import make_session_factory, session_scope
from furnest.logging import get_logger
from furnest.notify.interactions import (
    Interaction,
    InteractionType,
    JsonObject,
    deferred_update,
    ephemeral,
    is_fresh,
    pong,
    refusal,
    verify_signature,
)
from furnest.web.discord_dispatch import InteractionDispatcher

log = get_logger(__name__)

router = APIRouter()

SIGNATURE_HEADER = "X-Signature-Ed25519"
TIMESTAMP_HEADER = "X-Signature-Timestamp"


@router.post("/discord/interactions")
async def interactions(request: Request) -> JSONResponse:
    settings: Settings = request.app.state.settings
    public_key = settings.discord.public_key
    if public_key is None:
        raise HTTPException(status_code=404)

    signature = request.headers.get(SIGNATURE_HEADER)
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    body = await request.body()
    now = utcnow()
    if (
        signature is None
        or timestamp is None
        or not is_fresh(
            timestamp, now=now.timestamp(), max_age=settings.discord.interaction_max_age_seconds
        )
        or not verify_signature(public_key, signature, timestamp, body)
    ):
        log.warning("discord_interaction_rejected", reason="bad or stale signature")
        raise HTTPException(status_code=401, detail="invalid request signature")

    try:
        interaction = Interaction.from_json(json.loads(body))
    except ValueError as exc:  # json.JSONDecodeError is a ValueError too
        raise HTTPException(status_code=400, detail="malformed interaction") from exc
    if interaction.type is InteractionType.PING:
        return JSONResponse(pong())
    refused = refusal(
        interaction,
        channel_id=settings.discord.channel_id,
        allowed_user_ids=settings.discord.allowed_user_ids,
    )
    if refused is not None:
        log.warning(
            "discord_interaction_refused",
            reason=refused,
            interaction_id=interaction.id,
            user_id=interaction.user_id,
            channel_id=interaction.channel_id,
        )
        return JSONResponse(ephemeral("You're not allowed to act on furnest listings here."))

    def handle() -> tuple[JsonObject, bool]:
        dispatcher: InteractionDispatcher = request.app.state.discord_dispatcher
        with session_scope(make_session_factory(request.app.state.engine)) as session:
            if not record_interaction(session, interaction, now=now):
                return deferred_update(), True
            return dispatcher.dispatch(session, interaction), False

    response, replay = await run_in_threadpool(handle)
    log.info(
        "discord_interaction",
        interaction_id=interaction.id,
        interaction_type=interaction.type.name,
        action=interaction.action.value if interaction.action else None,
        property_id=interaction.property_id,
        replay=replay,
    )
    return JSONResponse(response)
