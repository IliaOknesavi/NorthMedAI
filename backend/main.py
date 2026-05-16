"""
NorthMedAI Backend — FastAPI entry point

Запуск: uvicorn main:app --reload
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from transcript import get_transcript, extract_video_id
# from detector import analyze_claims  # раскомментировать когда detector готов

app = FastAPI(title="NorthMedAI API", version="0.1.0")

# Разрешаем запросы от расширения Chrome
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # chrome-extension://* + localhost для разработки
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeRequest(BaseModel):
    video_id: str          # YouTube video ID или полный URL


class AnalyzeResponse(BaseModel):
    video_id: str
    transcript_snippets: int   # сколько сниппетов получили
    claims: list               # список утверждений с таймкодами


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest):
    # Нормализуем video_id
    try:
        video_id = extract_video_id(req.video_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Получаем субтитры (локально — без блокировок)
    try:
        snippets = get_transcript(video_id)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Субтитры недоступны: {e}")

    if not snippets:
        raise HTTPException(status_code=422, detail="Субтитры пустые")

    # TODO: передать snippets в detector.py → YandexGPT
    # claims = await analyze_claims(snippets)
    claims = []  # заглушка до готовности detector

    return AnalyzeResponse(
        video_id=video_id,
        transcript_snippets=len(snippets),
        claims=claims,
    )
