from fastapi import FastAPI

from app.api.routes import health, webhook

app = FastAPI(title="JaaykatBi 2.0")

app.include_router(health.router)
app.include_router(webhook.router)
