"""FastAPI routes + Jinja templates - the browse/search UI.

Thin: every route just calls into `service` or `store` and renders a
template. Whether a read came from SQLite or fell through to Scryfall stays
invisible here too, same as it is in service.py - except for the two routes
where an offline cache MISS has nothing to show (`/search`, `/cards/{id}`),
which catch `ScryfallOffline` specifically and render a "you're offline"
notice instead of a generic error page.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, service, store
from .scryfall import ScryfallOffline


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"query": "", "results": [], "offline": False}
    )


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = ""):
    results: list[dict] = []
    offline = False
    if q.strip():
        try:
            results = service.search(q)
        except ScryfallOffline:
            offline = True
    context = {"query": q, "results": results, "offline": offline}
    template = "_results.html" if request.headers.get("HX-Request") else "index.html"
    return templates.TemplateResponse(request, template, context)


@app.get("/cards/{card_id}", response_class=HTMLResponse)
def card_detail(request: Request, card_id: str):
    try:
        card = service.get_card(card_id)
    except ScryfallOffline:
        return templates.TemplateResponse(request, "card.html", {"card": None, "offline": True})
    return templates.TemplateResponse(request, "card.html", {"card": card, "offline": False})


@app.get("/collection", response_class=HTMLResponse)
def collection_page(request: Request, refreshed: int | None = None):
    data = store.list_collection()
    return templates.TemplateResponse(
        request, "collection.html", {"rows": data["rows"], "total": data["total"], "refreshed": refreshed}
    )


@app.post("/collection")
def add_owned_route(
    card_id: str = Form(...),
    finish: str = Form("nonfoil"),
    quantity: int = Form(1),
    condition: str = Form(""),
):
    service.add_owned(card_id, quantity=quantity, finish=finish, condition=condition)
    return RedirectResponse(url="/collection", status_code=303)


@app.post("/collection/{collection_id}")
def update_quantity_route(collection_id: int, quantity: int = Form(...)):
    store.set_quantity(collection_id, quantity)
    return RedirectResponse(url="/collection", status_code=303)


@app.post("/collection/{collection_id}/delete")
def delete_collection_row_route(collection_id: int):
    store.remove_from_collection(collection_id)
    return RedirectResponse(url="/collection", status_code=303)


@app.post("/refresh-prices")
def refresh_prices_route():
    refreshed = service.refresh_prices()
    return RedirectResponse(url=f"/collection?refreshed={refreshed}", status_code=303)


@app.get("/api/search")
def api_search(q: str = ""):
    if not q.strip():
        return []
    try:
        return service.search(q)
    except ScryfallOffline:
        return JSONResponse({"error": "offline"}, status_code=503)
