# main.py

# Import libraries
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os

# Import modules
from routers import auth, websocket, chats, files, users
from database.singleton import Database

# Start server
app: FastAPI = FastAPI()

# Add routers
app.include_router(auth.router_authentication)
app.include_router(websocket.router_websockets)
app.include_router(chats.router_chats)
app.include_router(files.router_files)
app.include_router(users.router_users)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:3000", "http://localhost:3000",],
    allow_credentials= True,
    allow_methods=["*"],
    allow_headers=["*"],
)

load_dotenv()

@app.on_event("startup")
def startup_event():
    db: Database = Database()

@app.on_event("shutdown")
def shutdown_event():
    db: Database = Database()
    db.close_connection()

@app.get("/ison")
async def ison():
    return {"message": "Yeah, I'm on!"}

# Esta parte e deja hasta el final de este script
# por cómo funcionan las direcciones por defecto
# en FastAPI
app.mount("/", StaticFiles(directory="static/public", html=True), name="static/public")

# Inicia servidor:

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=os.getenv("APP_RELOAD", "true").lower() == "true",
    )

"""
Brief documentary:
- pip freeze > requirements.txt
- pip install "fastapi[all]
"""
