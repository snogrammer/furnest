"""The inquiry template editor, its live preview and the signature. All need auth and CSRF.

Templates are Jinja2 that the user writes, so every one is rendered in the sandbox
(:mod:`furnest.email.templates`) against the sample listing before it is saved: a
template that doesn't parse, names something that doesn't exist or reaches for
something the sandbox refuses answers 422 with the error, and isn't stored.

The preview renders the form as it stands, with the stored signature and the
active criteria's move-in date and lease length. With htmx the editor posts on load
and on every edit (debounced) and swaps in just the preview partial. Without
JavaScript the Preview button posts the same form and gets the whole editor back.

Handlers are plain ``def`` so FastAPI runs the blocking SQLite calls in its threadpool.
"""

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from furnest.db.criteria_store import CriteriaStore
from furnest.db.email_template_store import (
    ProtectedTemplateError,
    SqlEmailTemplateStore,
    TemplateNotFoundError,
)
from furnest.db.session import make_session_factory
from furnest.email.templates import (
    DEFAULT_BODY,
    DEFAULT_SUBJECT,
    DEFAULT_TEMPLATE_NAME,
    MAX_BODY_LENGTH,
    MAX_SIGNATURE_LENGTH,
    MAX_SUBJECT_LENGTH,
    VARIABLES,
    RenderedEmail,
    TemplateRenderError,
    render,
    sample_context,
)
from furnest.web.auth import require_auth
from furnest.web.csrf import require_csrf
from furnest.web.templating import templates

router = APIRouter(
    prefix="/email-templates", dependencies=[Depends(require_auth), Depends(require_csrf)]
)

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")
# Paths under /email-templates that a template name would shadow.
RESERVED_NAMES = frozenset({"new", "preview", "signature"})
# Form slack over the editor's limits, so save can say what's wrong; anything
# bigger is refused before it is read. Over-limit templates are never rendered.
_SUBJECT_FORM_MAX = MAX_SUBJECT_LENGTH + 1_000
_BODY_FORM_MAX = MAX_BODY_LENGTH + 1_000
TOO_LONG_TO_PREVIEW = "Too long to preview."


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _store(request: Request) -> SqlEmailTemplateStore:
    return SqlEmailTemplateStore(make_session_factory(request.app.state.engine))


def _preview(
    request: Request, store: SqlEmailTemplateStore, subject: str, body: str
) -> RenderedEmail:
    active = CriteriaStore(make_session_factory(request.app.state.engine)).active()
    context = sample_context(active.criteria if active else None, store.signature())
    return render(subject, body, context)


def _render_index(
    request: Request,
    store: SqlEmailTemplateStore,
    *,
    flash: str | None = None,
    error: str | None = None,
    signature: str | None = None,
    status_code: int = 200,
) -> Response:
    context = {
        "templates": store.list(),
        "signature": store.signature() if signature is None else signature,
        "max_signature": MAX_SIGNATURE_LENGTH,
        "default_name": DEFAULT_TEMPLATE_NAME,
        "flash": flash,
        "error": error,
    }
    return templates.TemplateResponse(
        request, "email_templates/index.html", context, status_code=status_code
    )


def _render_editor(
    request: Request,
    store: SqlEmailTemplateStore,
    *,
    name: str,
    subject: str,
    body: str,
    is_new: bool,
    errors: dict[str, str] | None = None,
    flash: str | None = None,
    status_code: int = 200,
) -> Response:
    preview: RenderedEmail | None = None
    preview_error: str | None = None
    if len(subject) > MAX_SUBJECT_LENGTH or len(body) > MAX_BODY_LENGTH:
        preview_error = TOO_LONG_TO_PREVIEW
    else:
        try:
            preview = _preview(request, store, subject, body)
        except TemplateRenderError as exc:
            preview_error = str(exc)
    context: dict[str, Any] = {
        "name": name,
        "subject": subject,
        "body": body,
        "is_new": is_new,
        "is_default": name == DEFAULT_TEMPLATE_NAME,
        "errors": errors or {},
        "flash": flash,
        "preview": preview,
        "preview_error": preview_error,
        "variables": VARIABLES,
        "max_subject": MAX_SUBJECT_LENGTH,
        "max_body": MAX_BODY_LENGTH,
    }
    return templates.TemplateResponse(
        request, "email_templates/edit.html", context, status_code=status_code
    )


@router.get("", response_class=HTMLResponse)
def index(
    request: Request,
    deleted: Annotated[str | None, Query(max_length=100)] = None,
    signature: Annotated[int | None, Query()] = None,
) -> Response:
    flash = None
    if deleted is not None:
        flash = f"Deleted {deleted}."
    elif signature is not None:
        flash = "Signature saved."
    return _render_index(request, _store(request), flash=flash)


@router.get("/new", response_class=HTMLResponse)
def new(request: Request) -> Response:
    store = _store(request)
    default = store.get(DEFAULT_TEMPLATE_NAME)
    return _render_editor(
        request,
        store,
        name="",
        subject=default.subject if default else DEFAULT_SUBJECT,
        body=default.body if default else DEFAULT_BODY,
        is_new=True,
    )


@router.post("", response_class=HTMLResponse)
def save(
    request: Request,
    name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form(max_length=_SUBJECT_FORM_MAX)] = "",
    body: Annotated[str, Form(max_length=_BODY_FORM_MAX)] = "",
    is_new: Annotated[bool, Form()] = False,
) -> Response:
    store = _store(request)
    name = name.strip()
    body = body.replace("\r\n", "\n")
    errors: dict[str, str] = {}
    conflict = False
    if not NAME_PATTERN.fullmatch(name) or name in RESERVED_NAMES:
        errors["name"] = (
            "Use 1 to 100 lowercase letters, digits, - or _, starting with a letter or digit"
            f" (not {', '.join(sorted(RESERVED_NAMES))})."
        )
    elif is_new and store.get(name) is not None:
        errors["name"] = f"There is already a template named {name}."
        conflict = True
    elif not is_new and store.get(name) is None:
        raise HTTPException(status_code=404, detail="no such email template")
    if not subject.strip():
        errors["subject"] = "The subject can't be empty."
    elif len(subject) > MAX_SUBJECT_LENGTH:
        errors["subject"] = f"Keep the subject under {MAX_SUBJECT_LENGTH} characters."
    if not body.strip():
        errors["body"] = "The body can't be empty."
    elif len(body) > MAX_BODY_LENGTH:
        errors["body"] = f"Keep the body under {MAX_BODY_LENGTH:,} characters."
    if not errors:
        try:
            _preview(request, store, subject, body)
        except TemplateRenderError as exc:
            errors[exc.part] = f"Doesn't render: {exc}"
    if errors:
        return _render_editor(
            request,
            store,
            name=name,
            subject=subject,
            body=body,
            is_new=is_new,
            errors=errors,
            status_code=409 if conflict else 422,
        )
    store.save(name, subject=subject, body=body)
    return RedirectResponse(f"/email-templates/{name}?saved=1", status_code=303)


@router.post("/preview", response_class=HTMLResponse)
def preview(
    request: Request,
    name: Annotated[str, Form()] = "",
    subject: Annotated[str, Form(max_length=_SUBJECT_FORM_MAX)] = "",
    body: Annotated[str, Form(max_length=_BODY_FORM_MAX)] = "",
    is_new: Annotated[bool, Form()] = False,
) -> Response:
    store = _store(request)
    body = body.replace("\r\n", "\n")
    if not _is_htmx(request):
        return _render_editor(
            request, store, name=name.strip(), subject=subject, body=body, is_new=is_new
        )
    context: dict[str, Any] = {"preview": None, "preview_error": None}
    if len(subject) > MAX_SUBJECT_LENGTH or len(body) > MAX_BODY_LENGTH:
        context["preview_error"] = TOO_LONG_TO_PREVIEW
    else:
        try:
            context["preview"] = _preview(request, store, subject, body)
        except TemplateRenderError as exc:
            context["preview_error"] = str(exc)
    return templates.TemplateResponse(request, "email_templates/_preview.html", context)


@router.post("/signature", response_class=HTMLResponse)
def save_signature(request: Request, signature: Annotated[str, Form()] = "") -> Response:
    store = _store(request)
    signature = signature.replace("\r\n", "\n").strip()
    if len(signature) > MAX_SIGNATURE_LENGTH:
        return _render_index(
            request,
            store,
            error=f"Keep the signature under {MAX_SIGNATURE_LENGTH:,} characters.",
            signature=signature,
            status_code=422,
        )
    store.set_signature(signature)
    return RedirectResponse("/email-templates?signature=1", status_code=303)


@router.get("/{name}", response_class=HTMLResponse)
def edit(request: Request, name: str, saved: Annotated[int | None, Query()] = None) -> Response:
    store = _store(request)
    stored = store.get(name)
    if stored is None:
        raise HTTPException(status_code=404, detail="no such email template")
    return _render_editor(
        request,
        store,
        name=stored.name,
        subject=stored.subject,
        body=stored.body,
        is_new=False,
        flash="Saved." if saved is not None else None,
    )


@router.post("/{name}/delete")
def delete(request: Request, name: str) -> Response:
    store = _store(request)
    try:
        store.delete(name)
    except TemplateNotFoundError:
        raise HTTPException(status_code=404, detail="no such email template") from None
    except ProtectedTemplateError as exc:
        return _render_index(request, store, error=f"{exc}.", status_code=409)
    return RedirectResponse(f"/email-templates?deleted={name}", status_code=303)
