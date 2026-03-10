from fastapi import FastAPI

app = FastAPI(title="ClawAPI", version="1.0.0")

@app.get("/")
def root():
    return {"status": "ok", "service": "ClawAPI"}

@app.get("/health")
def health():
    return {"status": "healthy"}
