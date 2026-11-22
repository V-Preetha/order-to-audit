from fastapi import FastAPI

app = FastAPI(title="Order-to-Audit")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
