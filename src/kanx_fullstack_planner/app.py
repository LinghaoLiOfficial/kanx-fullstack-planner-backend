from fastapi import FastAPI

from .core.application import create_app

app: FastAPI = create_app()
