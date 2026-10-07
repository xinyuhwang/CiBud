from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from cibud import __version__
from cibud.api.routes import router
from cibud.db.repo import NotFound
from cibud.models.paper import InvalidTransition


class Health(BaseModel):
    status: str
    version: str


def create_app() -> FastAPI:
    app = FastAPI(title="CiBud API", version=__version__)
    app.include_router(router)

    @app.get("/health")
    def health() -> Health:
        return Health(status="ok", version=__version__)

    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": f"{exc} not found"}, status.HTTP_404_NOT_FOUND)

    @app.exception_handler(InvalidTransition)
    def invalid_transition(request: Request, exc: InvalidTransition) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status.HTTP_409_CONFLICT)

    return app


app = create_app()
