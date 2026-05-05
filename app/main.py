from __future__ import annotations

import secrets
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings, get_settings
from .models import MoveSelectedRequest, SearchResponse
from .qbit import QbitClient, jellyfin_target_suggestions_with_llm, qbit_health
from .search import load_indexers, search_and_enrich

settings = get_settings()
app = FastAPI(title=settings.app_name)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, same_site="strict", https_only=False)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

SEARCH_CACHE: dict[str, SearchResponse] = {}


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get("authenticated"))


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    settings = get_settings()
    if secrets.compare_digest(username, settings.web_username) and secrets.compare_digest(password, settings.web_password):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "用户名或密码错误"}, status_code=401)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health(_: None = Depends(require_login)):
    settings = get_settings()
    jackett = {"ok": False}
    llm = {"ok": False}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{settings.jackett_url.rstrip('/')}/api/v2.0/indexers/internetarchive/results",
                params={"apikey": settings.jackett_api_key, "Query": "ubuntu"},
            )
            r.raise_for_status()
            jackett = {"ok": True, "indexers": len(load_indexers(settings))}
    except Exception as exc:
        jackett = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{settings.llm_base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {settings.llm_api_key}"})
            r.raise_for_status()
            llm = {"ok": True, "model": settings.llm_model}
    except Exception as exc:
        llm = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return {"jackett": jackett, "qbit": await qbit_health(settings), "llm": llm}


@app.post("/api/search")
async def api_search(payload: dict, _: None = Depends(require_login)):
    query = str(payload.get("query") or "").strip()
    category = str(payload.get("category") or "all")
    sort = str(payload.get("sort") or "seeders")
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="请输入至少两个字符")
    response = await search_and_enrich(get_settings(), query, category)
    if sort == "size":
        response.results.sort(key=lambda r: r.size or -1, reverse=True)
    elif sort == "date":
        response.results.sort(key=lambda r: r.publish_date or "", reverse=True)
    elif sort == "sources":
        response.results.sort(key=lambda r: len(r.sources), reverse=True)
    else:
        response.results.sort(key=lambda r: r.seeders or -1, reverse=True)
    SEARCH_CACHE[query] = response
    return response


@app.post("/api/qbit/add")
async def api_add(payload: dict, _: None = Depends(require_login)):
    query = str(payload.get("query") or "")
    token = str(payload.get("token") or "")
    cached = SEARCH_CACHE.get(query)
    if not cached:
        raise HTTPException(status_code=404, detail="搜索结果已过期，请重新搜索")
    result = next((r for r in cached.results if r.token == token), None)
    if not result:
        raise HTTPException(status_code=404, detail="找不到该结果，请重新搜索")
    client = QbitClient(get_settings())
    try:
        await client.add_result(result)
    finally:
        await client.close()
    return JSONResponse({"ok": True, "message": "已添加到下载", "title": result.title})


@app.get("/api/qbit/torrents")
async def api_qbit_torrents(_: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        return {"torrents": await client.torrents()}
    finally:
        await client.close()


@app.get("/api/qbit/torrents/{torrent_hash}/files")
async def api_qbit_files(torrent_hash: str, _: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        return {"files": await client.file_tree(torrent_hash)}
    finally:
        await client.close()


@app.post("/api/qbit/torrents/{torrent_hash}/start")
async def api_qbit_start(torrent_hash: str, _: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        await client.start_torrent(torrent_hash)
        return {"ok": True, "message": "已开始 qB 任务"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:240]}") from exc
    finally:
        await client.close()


@app.post("/api/qbit/torrents/{torrent_hash}/stop")
async def api_qbit_stop(torrent_hash: str, _: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        await client.stop_torrent(torrent_hash)
        return {"ok": True, "message": "已停止 qB 任务"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:240]}") from exc
    finally:
        await client.close()


@app.post("/api/qbit/torrents/{torrent_hash}/delete-with-files")
async def api_qbit_delete_with_files(torrent_hash: str, _: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        await client.delete_torrent(torrent_hash, delete_files=True)
        return {"ok": True, "message": "已删除 qB 任务及文件"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:240]}") from exc
    finally:
        await client.close()


@app.get("/api/jellyfin/targets")
async def api_jellyfin_targets(query: str, _: None = Depends(require_login)):
    return {"targets": await jellyfin_target_suggestions_with_llm(query, get_settings())}


@app.post("/api/qbit/torrents/{torrent_hash}/move-selected")
async def api_qbit_move_selected(torrent_hash: str, payload: MoveSelectedRequest, _: None = Depends(require_login)):
    client = QbitClient(get_settings())
    try:
        moved = await client.move_selected(
            torrent_hash,
            payload.selected_paths,
            payload.target_category,
            payload.target_folder,
        )
        return {"ok": True, "moved": moved, "message": f"已移动 {len(moved)} 项，并删除 qB 任务及剩余文件"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {str(exc)[:240]}") from exc
    finally:
        await client.close()
