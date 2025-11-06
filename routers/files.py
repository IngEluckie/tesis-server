# files.py

"""
Router that manages requests, routing, and managing files, images, videos, etc..
"""

# Import libraries
from fastapi import APIRouter

# Import modules
from static.protected.fileManager import ProfileImage

router_files: APIRouter = APIRouter(prefix="/files")

@router_files.get("/ison")
async def ison():
    return {"message": "Yeah! I'm on!"}