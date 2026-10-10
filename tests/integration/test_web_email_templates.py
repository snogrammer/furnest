"""The email template editor, live preview and signature form, end to end."""

from collections.abc import Callable
from datetime import date

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.criteria_store import CriteriaStore
from furnest.db.email_template_store import SqlEmailTemplateStore
from furnest.domain import Criteria
from furnest.email.templates import DEFAULT_SUBJECT, DEFAULT_TEMPLATE_NAME

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
Csrf = Callable[[str], str]


@pytest.fixture
def store(session_factory: sessionmaker[Session]) -> SqlEmailTemplateStore:
    return SqlEmailTemplateStore(session_factory)


def token(client: TestClient, csrf: Csrf) -> str:
    return csrf(client.get("/email-templates").text)


def post(
    client: TestClient, csrf: Csrf, path: str, headers: dict[str, str] | None = None, **fields: str
) -> Response:
    data = {"csrf_token": token(client, csrf), **fields}
    return client.post(path, data=data, headers=headers, follow_redirects=False)


# --- access -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/email-templates", "/email-templates/new", "/email-templates/default"]
)
def test_pages_need_auth(client: TestClient, path: str) -> None:
    assert client.get(path, follow_redirects=False).status_code in (303, 401)


def test_posts_need_csrf(authed_client: TestClient) -> None:
    for path in (
        "/email-templates",
        "/email-templates/preview",
        "/email-templates/signature",
        "/email-templates/x/delete",
    ):
        assert authed_client.post(path, data={}).status_code == 403


def test_nav_links_to_the_editor(authed_client: TestClient) -> None:
    page = authed_client.get("/email-templates")
    assert 'href="/email-templates" aria-current="page">Email</a>' in page.text


# --- index and signature ------------------------------------------------------


def test_index_lists_templates(authed_client: TestClient, store: SqlEmailTemplateStore) -> None:
    store.save("follow-up", subject="Following up", body="b")

    page = authed_client.get("/email-templates")

    assert page.status_code == 200
    assert 'href="/email-templates/default"' in page.text
    assert 'href="/email-templates/follow-up"' in page.text
    assert 'action="/email-templates/follow-up/delete"' in page.text
    assert 'action="/email-templates/default/delete"' not in page.text


def test_save_signature(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    response = post(
        authed_client, csrf, "/email-templates/signature", signature="  A. Person\r\nNumber  \n"
    )

    assert response.status_code == 303
    assert store.signature() == "A. Person\nNumber"
    page = authed_client.get("/email-templates?signature=1")
    assert "Signature saved." in page.text
    assert "A. Person\nNumber</textarea>" in page.text


def test_signature_too_long(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    response = post(authed_client, csrf, "/email-templates/signature", signature="x" * 2001)

    assert response.status_code == 422
    assert store.signature() == ""


# --- editor -------------------------------------------------------------------------


def test_editor_shows_template_and_preview(
    authed_client: TestClient, store: SqlEmailTemplateStore, session_factory: sessionmaker[Session]
) -> None:
    store.set_signature("A. Person")
    CriteriaStore(session_factory).save(
        Criteria(move_in=date(2027, 8, 9), move_out=date(2027, 11, 9)), created_by="test"
    )

    page = authed_client.get("/email-templates/default")

    assert page.status_code == 200
    assert "Inquiry about {{ listing.title }}" in page.text
    assert "Inquiry about Sunny furnished 2BR near the park" in page.text
    assert "August 9, 2027 for 3 months." in page.text
    assert "A. Person" in page.text
    assert "listing.price" in page.text  # the variables list


def test_unknown_template_is_404(authed_client: TestClient) -> None:
    assert authed_client.get("/email-templates/missing").status_code == 404


def test_new_starts_from_the_default(authed_client: TestClient) -> None:
    page = authed_client.get("/email-templates/new")

    assert page.status_code == 200
    assert 'name="is_new" value="true"' in page.text
    assert 'id="name" name="name" value=""' in page.text
    assert DEFAULT_SUBJECT in page.text


def test_create_template(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates",
        name="short",
        subject="About {{ listing.title }}",
        body="Hello\r\n{{ signature }}",
        is_new="true",
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/email-templates/short?saved=1"
    stored = store.get("short")
    assert stored is not None
    assert stored.body == "Hello\n{{ signature }}"
    assert "Saved." in authed_client.get("/email-templates/short?saved=1").text


def test_create_existing_name_is_409(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates",
        name="default",
        subject="s",
        body="b",
        is_new="true",
    )

    assert response.status_code == 409
    assert "already a template named default" in response.text


@pytest.mark.parametrize("name", ["", "Bad Name", "-dash", "new", "preview", "x" * 101])
def test_bad_names_are_422(authed_client: TestClient, csrf: Csrf, name: str) -> None:
    response = post(
        authed_client, csrf, "/email-templates", name=name, subject="s", body="b", is_new="true"
    )

    assert response.status_code == 422
    assert 'id="name-error"' in response.text


def test_update_template(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates",
        name="default",
        subject="New {{ listing.city }}",
        body="b",
    )

    assert response.status_code == 303
    stored = store.get(DEFAULT_TEMPLATE_NAME)
    assert stored is not None
    assert stored.subject == "New {{ listing.city }}"


def test_update_missing_template_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(authed_client, csrf, "/email-templates", name="ghost", subject="s", body="b")

    assert response.status_code == 404


@pytest.mark.parametrize(
    ("subject", "body", "field", "message"),
    [
        ("", "b", "subject", "can&#39;t be empty"),
        ("s", "  ", "body", "can&#39;t be empty"),
        ("s" * 301, "b", "subject", "under 300"),
        ("s", "b" * 20_001, "body", "under 20,000"),
        ("{{ listing.titel }}", "b", "subject", "Doesn&#39;t render"),
        ("s", "{{ listing.__class__ }}", "body", "unsafe"),
        ("s", "{% if %}", "body", "line 1"),
    ],
)
def test_invalid_template_is_not_saved(
    authed_client: TestClient,
    csrf: Csrf,
    store: SqlEmailTemplateStore,
    subject: str,
    body: str,
    field: str,
    message: str,
) -> None:
    response = post(
        authed_client, csrf, "/email-templates", name="default", subject=subject, body=body
    )

    assert response.status_code == 422
    assert f'id="{field}-error"' in response.text
    assert message in response.text
    stored = store.get(DEFAULT_TEMPLATE_NAME)
    assert stored is not None
    assert stored.subject == DEFAULT_SUBJECT


# --- preview ------------------------------------------------------------------------


def test_htmx_preview_returns_the_partial(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    store.set_signature("A. Person")

    response = post(
        authed_client,
        csrf,
        "/email-templates/preview",
        headers=HTMX,
        subject="Re: {{ listing.title }}",
        body="<b>{{ listing.price }}</b>\n{{ signature }}",
    )

    assert response.status_code == 200
    text = response.text
    assert "<html" not in text
    assert "Re: Sunny furnished 2BR near the park" in text
    # Plain-text output, shown escaped in the page.
    assert "&lt;b&gt;$2,400.00&lt;/b&gt;\nA. Person" in text


def test_htmx_preview_reports_errors(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates/preview",
        headers=HTMX,
        subject="s",
        body="{{ ''.__class__.__mro__ }}",
    )

    assert response.status_code == 200
    assert 'class="form-error"' in response.text
    assert "unsafe" in response.text


def test_htmx_preview_refuses_oversized_input(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates/preview",
        headers=HTMX,
        subject="s",
        body="b" * 20_001,
    )

    assert "Too long to preview." in response.text


@pytest.mark.parametrize("path", ["/email-templates", "/email-templates/preview"])
def test_over_limit_template_is_not_rendered(
    authed_client: TestClient, csrf: Csrf, path: str
) -> None:
    # Over the editor limit but within the form slack: the editor comes back
    # without rendering it (a long template could be slow to render).
    response = post(
        authed_client, csrf, path, name="big", is_new="true", subject="s", body="{{ 1 }}" * 2_900
    )

    assert "Too long to preview." in response.text


def test_far_oversized_form_is_refused_unread(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(
        authed_client, csrf, "/email-templates", name="big", subject="s", body="b" * 30_000
    )

    assert response.status_code == 422


def test_preview_without_htmx_returns_the_editor(authed_client: TestClient, csrf: Csrf) -> None:
    response = post(
        authed_client,
        csrf,
        "/email-templates/preview",
        name="draft",
        subject="Draft {{ listing.city }}",
        body="b",
        is_new="true",
    )

    assert response.status_code == 200
    text = response.text
    assert "<html" in text
    assert 'value="draft"' in text
    assert "Draft Springfield" in text


# --- delete ---------------------------------------------------------------------------


def test_delete_template(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    store.save("old", subject="s", body="b")

    response = post(authed_client, csrf, "/email-templates/old/delete")

    assert response.status_code == 303
    assert store.get("old") is None
    assert "Deleted old." in authed_client.get("/email-templates?deleted=old").text


def test_delete_default_is_409(
    authed_client: TestClient, csrf: Csrf, store: SqlEmailTemplateStore
) -> None:
    response = post(authed_client, csrf, "/email-templates/default/delete")

    assert response.status_code == 409
    assert "can be edited but not deleted" in response.text
    assert store.get(DEFAULT_TEMPLATE_NAME) is not None


def test_delete_missing_is_404(authed_client: TestClient, csrf: Csrf) -> None:
    assert post(authed_client, csrf, "/email-templates/ghost/delete").status_code == 404
