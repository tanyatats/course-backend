import os
from app.main import app

# Позволяет запускать и через `python main.py` — порт берётся из окружения ($PORT),
# как того требует Timeweb App Platform. По умолчанию 8000 для локального запуска.
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port)
