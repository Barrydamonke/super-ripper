"""Drives page (placeholder until Task 7)."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/drives", response_class=HTMLResponse)
def drives_page():
    return "<p>Drives</p>"


@router.post("/drives/rescan", response_class=HTMLResponse)
def drives_rescan():
    return "<p>Drives</p>"
