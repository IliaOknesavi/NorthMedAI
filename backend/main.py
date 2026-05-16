"""
NorthMedAI Backend — FastAPI entry point
"""
from fastapi import FastAPI

app = FastAPI(title="NorthMedAI API")


@app.get("/health")
def health():
    return {"status": "ok"}


# TODO: implement /analyze endpoint
