import logging

from fastapi import FastAPI

from app.api.routes import health, webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="JaaykatBi 2.0")

app.include_router(health.router)
app.include_router(webhook.router)
