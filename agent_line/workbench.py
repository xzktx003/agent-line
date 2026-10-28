"""Same-origin Chinese workbench; privileged APIs retain authentication."""

from pathlib import Path
from fastapi.staticfiles import StaticFiles


def mount_workbench(app, directory):
    directory = Path(directory).resolve(strict=True)
    if not (directory / "index.html").is_file():
        raise ValueError("Workbench index is missing")
    app.mount(
        "/workbench", StaticFiles(directory=directory, html=True), name="workbench"
    )

    @app.middleware("http")
    async def workbench_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/workbench/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            response.headers["Cache-Control"] = "no-store"
        return response
