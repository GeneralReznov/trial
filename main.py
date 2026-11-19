from __future__ import annotations
import os
import uvicorn
from app.main import app
def run() -> None:
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "5000")),
    )
if __name__ == "__main__":
    run()   