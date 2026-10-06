"""DiscordClient: request shape, rate-limit buckets, 429s, retries and error mapping.

Discord is faked with ``httpx2.MockTransport``. Time is a fake clock whose sleep
advances it, and jitter comes from a fixed ``rng``, so every wait is exact.
"""

import json
from typing import Any

import httpx2
import pytest

from furnest import __version__
from furnest.config import Settings
from furnest.notify.discord import (
    MAX_RATE_LIMIT_WAITS,
    DiscordClient,
    DiscordError,
    DiscordMessage,
    DiscordNotFoundError,
    DiscordRateLimitedError,
    discord_client,
    user_agent,
)

pytestmark = pytest.mark.unit

TOKEN = "bot-token-that-must-stay-secret"  # noqa: S105 - a fake
CHANNEL = "111"
API = "https://discord.com/api/v10"

Reply = httpx2.Response | Exception


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class Discord:
    """A MockTransport handler that answers with ``replies`` in order."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def message(message_id: str = "900", channel_id: str = CHANNEL, **headers: str) -> httpx2.Response:
    return httpx2.Response(200, json={"id": message_id, "channel_id": channel_id}, headers=headers)


def rate_limited(
    retry_after: float, *, is_global: bool = False, headers: dict[str, str] | None = None
) -> httpx2.Response:
    body = {"message": "Rate limited.", "retry_after": retry_after, "global": is_global}
    return httpx2.Response(429, json=body, headers=headers)


def make_client(discord: Discord, fake: FakeTime, **kwargs: Any) -> DiscordClient:  # noqa: ANN401
    return DiscordClient(
        TOKEN,
        user_agent="DiscordBot (https://furnest.example.com, 0)",
        transport=httpx2.MockTransport(discord),
        clock=fake.clock,
        sleep=fake.sleep,
        rng=lambda: 0.5,
        **kwargs,
    )


@pytest.fixture
def fake() -> FakeTime:
    return FakeTime()


PAYLOAD = {"content": "hi", "allowed_mentions": {"parse": []}}


# --- Requests ---------------------------------------------------------------------


def test_create_message_posts_json_with_bot_auth(fake: FakeTime) -> None:
    discord = Discord(message("900"))
    with make_client(discord, fake) as client:
        result = client.create_message(CHANNEL, PAYLOAD)

    assert result == DiscordMessage(id="900", channel_id=CHANNEL)
    (request,) = discord.requests
    assert request.method == "POST"
    assert str(request.url) == f"{API}/channels/{CHANNEL}/messages"
    assert request.headers["authorization"] == f"Bot {TOKEN}"
    assert request.headers["user-agent"] == "DiscordBot (https://furnest.example.com, 0)"
    assert json.loads(request.content) == PAYLOAD
    assert fake.slept == []


def test_edit_message_patches_the_message(fake: FakeTime) -> None:
    discord = Discord(message("900"))
    client = make_client(discord, fake)

    result = client.edit_message(CHANNEL, "900", PAYLOAD)

    assert result == DiscordMessage(id="900", channel_id=CHANNEL)
    (request,) = discord.requests
    assert request.method == "PATCH"
    assert str(request.url) == f"{API}/channels/{CHANNEL}/messages/900"


def test_channel_id_falls_back_to_the_one_asked_for(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(200, json={"id": "900"}))
    assert make_client(discord, fake).create_message(CHANNEL, PAYLOAD).channel_id == CHANNEL


def test_user_agent_follows_discords_format() -> None:
    assert user_agent("https://furnest.example.com/") == (
        f"DiscordBot (https://furnest.example.com, {__version__})"
    )


# --- Rate limits ------------------------------------------------------------------


def test_exhausted_bucket_waits_before_the_next_request_on_that_route(fake: FakeTime) -> None:
    exhausted = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"}
    discord = Discord(message(**exhausted), message("901"), message("902"))
    client = make_client(discord, fake)

    client.create_message(CHANNEL, PAYLOAD)
    client.create_message("222", PAYLOAD)  # another channel: its own bucket
    assert fake.slept == []
    client.create_message(CHANNEL, PAYLOAD)

    assert fake.slept == [2.5]


def test_bucket_with_requests_left_does_not_wait(fake: FakeTime) -> None:
    headers = {"X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "2.5"}
    discord = Discord(message(**headers), message())
    client = make_client(discord, fake)

    client.create_message(CHANNEL, PAYLOAD)
    client.create_message(CHANNEL, PAYLOAD)

    assert fake.slept == []


def test_bucket_that_already_reset_does_not_wait(fake: FakeTime) -> None:
    exhausted = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "1"}
    discord = Discord(message(**exhausted), message())
    client = make_client(discord, fake)

    client.create_message(CHANNEL, PAYLOAD)
    fake.now += 5
    client.create_message(CHANNEL, PAYLOAD)

    assert fake.slept == []


def test_429_waits_retry_after_from_the_body_then_succeeds(fake: FakeTime) -> None:
    discord = Discord(rate_limited(1.25, headers={"Retry-After": "9"}), message())

    result = make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert result.id == "900"
    assert fake.slept == [1.25]
    assert len(discord.requests) == 2


def test_429_falls_back_to_the_retry_after_header(fake: FakeTime) -> None:
    discord = Discord(
        httpx2.Response(429, text="slow down", headers={"Retry-After": "3"}), message()
    )

    make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert fake.slept == [3.0]


@pytest.mark.parametrize("bad", [[1], "soon", True, -5])
def test_429_with_a_malformed_body_value_uses_the_header(fake: FakeTime, bad: object) -> None:
    reply = httpx2.Response(429, json={"retry_after": bad}, headers={"Retry-After": "2"})

    make_client(Discord(reply, message()), fake).create_message(CHANNEL, PAYLOAD)

    assert fake.slept == [2.0]


def test_429_without_any_hint_waits_a_second(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(429), message())

    make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert fake.slept == [1.0]


def test_global_429_blocks_every_route(fake: FakeTime) -> None:
    discord = Discord(rate_limited(2, is_global=True), message(), message("901"))
    client = make_client(discord, fake)

    client.create_message(CHANNEL, PAYLOAD)
    fake.now -= 2  # pretend the retry happened instantly: the global block still holds
    client.edit_message("222", "5", PAYLOAD)

    assert fake.slept == [2.0, 2.0]


def test_global_header_counts_as_global(fake: FakeTime) -> None:
    discord = Discord(
        httpx2.Response(429, json={"retry_after": 2}, headers={"X-RateLimit-Global": "true"}),
        message(),
        message(),
    )
    client = make_client(discord, fake)

    client.create_message(CHANNEL, PAYLOAD)
    fake.now -= 2
    client.create_message("222", PAYLOAD)

    assert fake.slept == [2.0, 2.0]


def test_429s_do_not_use_up_retries(fake: FakeTime) -> None:
    discord = Discord(rate_limited(1), rate_limited(1), message())

    make_client(discord, fake, max_retries=0).create_message(CHANNEL, PAYLOAD)

    assert fake.slept == [1.0, 1.0]


def test_long_retry_after_gives_up_without_waiting(fake: FakeTime) -> None:
    discord = Discord(rate_limited(120))

    with pytest.raises(DiscordRateLimitedError) as caught:
        make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert caught.value.retry_after == 120
    assert caught.value.status == 429
    assert fake.slept == []


def test_endless_429s_give_up(fake: FakeTime) -> None:
    discord = Discord(*[rate_limited(0.1) for _ in range(MAX_RATE_LIMIT_WAITS + 1)])

    with pytest.raises(DiscordRateLimitedError):
        make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert len(fake.slept) == MAX_RATE_LIMIT_WAITS


# --- Retries and errors -----------------------------------------------------------


def test_5xx_and_transport_errors_are_retried_with_backoff(fake: FakeTime) -> None:
    discord = Discord(
        httpx2.Response(502), httpx2.ConnectError("refused"), httpx2.ReadTimeout("slow"), message()
    )

    result = make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert result.id == "900"
    # rng 0.5 * 1s * 2**attempt
    assert fake.slept == [0.5, 1.0, 2.0]


def test_5xx_gives_up_after_max_retries(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(503), httpx2.Response(503))

    with pytest.raises(DiscordError, match="HTTP 503 after 2 attempts") as caught:
        make_client(discord, fake, max_retries=1).create_message(CHANNEL, PAYLOAD)

    assert caught.value.status == 503


def test_transport_error_gives_up_after_max_retries(fake: FakeTime) -> None:
    discord = Discord(httpx2.ConnectError("refused"))

    with pytest.raises(DiscordError, match="ConnectError after 1 attempts") as caught:
        make_client(discord, fake, max_retries=0).create_message(CHANNEL, PAYLOAD)

    assert isinstance(caught.value.__cause__, httpx2.ConnectError)


def test_404_raises_not_found_with_discords_code(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(404, json={"code": 10008, "message": "Unknown Message"}))

    with pytest.raises(DiscordNotFoundError, match="Unknown Message") as caught:
        make_client(discord, fake).edit_message(CHANNEL, "900", PAYLOAD)

    assert caught.value.status == 404
    assert caught.value.code == 10008
    assert len(discord.requests) == 1


def test_other_4xx_raises_discord_error_without_retrying(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(400, json={"code": 50035, "message": "Invalid Form Body"}))

    with pytest.raises(DiscordError) as caught:
        make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert not isinstance(caught.value, DiscordNotFoundError)
    assert caught.value.code == 50035
    assert len(discord.requests) == 1


def test_4xx_without_json_body(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(401, text="401: Unauthorized"))

    with pytest.raises(DiscordError, match=r"HTTP 401$") as caught:
        make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert caught.value.code is None


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.Response(200, text="not json"),
        httpx2.Response(200, json=["a list"]),
        httpx2.Response(200, json={"no": "id"}),
    ],
)
def test_success_without_a_message_raises(fake: FakeTime, reply: httpx2.Response) -> None:
    with pytest.raises(DiscordError):
        make_client(Discord(reply), fake).create_message(CHANNEL, PAYLOAD)


def test_token_never_appears_in_errors(fake: FakeTime) -> None:
    discord = Discord(httpx2.Response(403, json={"code": 50001, "message": "Missing Access"}))

    with pytest.raises(DiscordError) as caught:
        make_client(discord, fake).create_message(CHANNEL, PAYLOAD)

    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value.args)


# --- From settings ----------------------------------------------------------------


def configured(**discord: Any) -> Settings:  # noqa: ANN401
    return Settings.model_validate(
        {"app": {"base_url": "https://furnest.example.com"}, "discord": discord}
    )


@pytest.mark.parametrize(
    "discord",
    [{}, {"bot_token": TOKEN}, {"channel_id": CHANNEL}, {"bot_token": TOKEN, "channel_id": ""}],
)
def test_discord_client_is_none_until_configured(discord: dict[str, str]) -> None:
    assert discord_client(configured(**discord)) is None


def test_discord_client_from_settings(fake: FakeTime) -> None:
    discord = Discord(message())
    settings = configured(bot_token=TOKEN, channel_id=CHANNEL, max_retries=0)

    client = discord_client(
        settings, transport=httpx2.MockTransport(discord), clock=fake.clock, sleep=fake.sleep
    )

    assert client is not None
    assert client.max_retries == 0
    client.create_message(CHANNEL, PAYLOAD)
    (request,) = discord.requests
    assert request.headers["authorization"] == f"Bot {TOKEN}"
    assert request.headers["user-agent"] == user_agent("https://furnest.example.com/")
