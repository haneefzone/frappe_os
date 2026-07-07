from fastapi import FastAPI

from app import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="FDM Platform", version=__version__)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
