import sys
import os

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import uvicorn
    print("Starting uvicorn on 127.0.0.1:8000...", flush=True)
    try:
        uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")
    except Exception as e:
        print("Uvicorn crash:", e, flush=True)
