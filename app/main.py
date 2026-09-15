from fastapi import FastAPI

app = FastAPI(title="Roadside Triage Agent")


@app.get("/health")
async def health():
    return {"status": "ok"}
