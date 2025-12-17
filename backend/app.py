from fastapi import FastAPI, HTTPException, BackgroundTasks, Query
from urllib.parse import quote, unquote
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict
import os
import sys
import shutil
from pathlib import Path
import time

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import config
from services.spotify import SpotifyService

def get_system_downloads_folder():
    """Get the user's system Downloads folder"""
    home = Path.home()
    
    # Check common Downloads folder locations
    if os.name == 'nt':  # Windows
        downloads = home / "Downloads"
    else:  # Linux/Mac
        downloads = home / "Downloads"
    
    # Create if doesn't exist
    downloads.mkdir(parents=True, exist_ok=True)
    return str(downloads)
from services.youtube import YouTubeService
from services.metadata import MetadataService
from services.navidrome import NavidromeService
from utils.file_handler import get_download_path

app = FastAPI(title="Music Downloader API", version="1.0.0")

# CORS middleware (still useful for API endpoints)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Get frontend directory path
BASE_DIR = Path(__file__).parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

# Serve static files (CSS, JS, images, etc.)
if FRONTEND_DIR.exists():
    # Serve CSS and JS files
    @app.get("/styles.css")
    async def get_styles():
        css_path = FRONTEND_DIR / "styles.css"
        if css_path.exists():
            return FileResponse(str(css_path), media_type="text/css")
        raise HTTPException(status_code=404)
    
    @app.get("/app.js")
    async def get_app_js():
        js_path = FRONTEND_DIR / "app.js"
        if js_path.exists():
            return FileResponse(str(js_path), media_type="application/javascript")
        raise HTTPException(status_code=404)

# Initialize services
try:
    spotify_service = SpotifyService()
except Exception as e:
    print(f"Warning: Spotify service initialization failed: {e}")
    spotify_service = None

youtube_service = YouTubeService()
metadata_service = MetadataService()
navidrome_service = NavidromeService()

# Request models
class SearchRequest(BaseModel):
    query: str
    limit: Optional[int] = 20

class DownloadRequest(BaseModel):
    track_id: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'
    video_id: Optional[str] = None  # YouTube video ID if user selected a specific candidate

class AlbumDownloadRequest(BaseModel):
    album_id: str
    location: Optional[str] = "local"  # 'local' or 'navidrome'

# Response models
class TrackResponse(BaseModel):
    id: str
    name: str
    artist: str
    artists: List[str]
    album: str
    duration_ms: int
    external_url: str
    preview_url: Optional[str]
    album_art: Optional[str]
    release_date: str

class DownloadStatusResponse(BaseModel):
    status: str
    message: str
    file_path: Optional[str] = None

# Download status storage (in production, use Redis or a database)
download_status: Dict[str, Dict] = {}

def download_and_process(track_id: str, location: str = "local", video_id: str = None):
    """Background task to download and process a track"""
    try:
        download_status[track_id] = {"status": "processing", "message": "Fetching track info...", "stage": "fetching", "progress": 10}
        
        # Get track details from Spotify
        track_info = spotify_service.get_track_details(track_id)
        if not track_info:
            download_status[track_id] = {
                "status": "error",
                "message": "Could not fetch track information",
                "progress": 0
            }
            return
        
        download_status[track_id] = {"status": "processing", "message": "Preparing download location...", "stage": "preparing", "progress": 15}
        
        # Determine download path based on location preference
        if location == "navidrome":
            # Download directly to Navidrome music directory with proper structure (Artist/Album/)
            # First download to temp location, then copy to Navidrome directory
            temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            download_path = get_download_path(track_info, temp_dir, config.OUTPUT_FORMAT)
            print(f"Downloading track {track_id} for Navidrome: {download_path}")
        else:
            # For local downloads: download to temp folder, then serve via browser download
            # This allows each user's browser to save to their own Downloads folder
            temp_dir = os.path.join(config.DOWNLOAD_DIR, "temp")
            Path(temp_dir).mkdir(parents=True, exist_ok=True)
            download_path = get_download_path(track_info, temp_dir, config.OUTPUT_FORMAT)
            print(f"Downloading track {track_id} for local browser download: {download_path}")
        
        download_status[track_id] = {"status": "processing", "message": "Searching YouTube and downloading...", "stage": "downloading", "progress": 30}
        
        # Download - pass full track_info for better matching
        # If video_id is provided, download that specific video
        download_result = youtube_service.search_and_download(
            track_info['name'],
            track_info['artist'],
            download_path,
            track_info,  # Pass full track info for validation
            video_id  # Specific YouTube video if user selected one
        )
        
        if not download_result.get('success'):
            download_status[track_id] = {
                "status": "error",
                "message": f"Download failed: {download_result.get('error', 'Unknown error')}",
                "progress": 0
            }
            return
        
        download_status[track_id] = {"status": "processing", "message": "Applying metadata...", "stage": "tagging", "progress": 85}
        
        # Apply metadata to downloaded file
        metadata_service.apply_metadata(download_result['file_path'], track_info)
        
        # Handle completion based on location
        if location == "navidrome":
            # Copy to Navidrome music directory with proper structure (Artist/Album/)
            download_status[track_id] = {"status": "processing", "message": "Copying to Navidrome library...", "stage": "copying", "progress": 90}
            
            try:
                # Get target path in Navidrome directory (Artist/Album/filename.mp3)
                target_path = navidrome_service.get_target_path(track_info, config.OUTPUT_FORMAT)
                
                # Copy file to Navidrome directory
                shutil.copy2(download_result['file_path'], target_path)
                
                # Clean up temp file
                if os.path.exists(download_result['file_path']):
                    os.remove(download_result['file_path'])
                
                # Trigger Navidrome scan
                navidrome_result = navidrome_service.finalize_track(str(target_path))
                
                if navidrome_result.get('success'):
                    download_status[track_id] = {
                        "status": "completed",
                        "message": "Track successfully added to Navidrome library",
                        "file_path": str(target_path),
                        "stage": "completed",
                        "progress": 100
                    }
                else:
                    download_status[track_id] = {
                        "status": "completed",
                        "message": f"Track added to library (scan may need manual trigger): {navidrome_result.get('error', '')}",
                        "file_path": str(target_path),
                        "stage": "completed",
                        "progress": 100
                    }
            except Exception as e:
                download_status[track_id] = {
                    "status": "error",
                    "message": f"Failed to copy to Navidrome: {str(e)}",
                    "progress": 0
                }
        else:
            # For local downloads, provide download URL for browser to handle
            # The file is ready, browser will download it to user's Downloads folder
            filename = os.path.basename(download_result['file_path'])
            # URL encode the filename to handle special characters (use query parameter)
            encoded_filename = quote(filename, safe='')
            download_url = f"/api/download/file/{track_id}?filename={encoded_filename}"
            download_status[track_id] = {
                "status": "completed",
                "message": "Track ready for download",
                "file_path": download_result['file_path'],
                "download_url": download_url,  # URL to trigger browser download
                "stage": "completed",
                "progress": 100
            }
    
    except Exception as e:
        download_status[track_id] = {
            "status": "error",
            "message": f"Error: {str(e)}",
            "progress": 0
        }

@app.get("/")
async def root():
    """Serve the frontend index.html"""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "Music Downloader API", "status": "running", "frontend": "not found"}

@app.post("/api/search", response_model=List[TrackResponse])
async def search_tracks(request: SearchRequest):
    """Search for tracks on Spotify"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        tracks = spotify_service.search_tracks(request.query, request.limit)
        return tracks
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search failed: {str(e)}")

@app.post("/api/search/albums")
async def search_albums(request: SearchRequest):
    """Search for albums on Spotify"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        albums = spotify_service.search_albums(request.query, request.limit)
        return albums
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Album search failed: {str(e)}")

@app.get("/api/album/{album_id}")
async def get_album(album_id: str):
    """Get album details including all tracks"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        album = spotify_service.get_album_details(album_id)
        if not album:
            raise HTTPException(status_code=404, detail="Album not found")
        return album
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching album: {str(e)}")

@app.get("/api/track/{track_id}", response_model=TrackResponse)
async def get_track(track_id: str):
    """Get details for a specific track"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        track = spotify_service.get_track_details(track_id)
        if not track:
            raise HTTPException(status_code=404, detail="Track not found")
        return track
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching track: {str(e)}")

@app.post("/api/download")
async def download_track(request: DownloadRequest, background_tasks: BackgroundTasks):
    """Start downloading a track"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    # Validate location
    if request.location not in ["local", "navidrome"]:
        request.location = "local"  # Default to local
    
    # Initialize status
    location_msg = "local downloads folder" if request.location == "local" else "Navidrome server"
    download_status[request.track_id] = {
        "status": "queued",
        "message": f"Download queued for {location_msg}",
        "progress": 0,
        "stage": "queued"
    }
    
    # Add background task with location and video_id parameters
    background_tasks.add_task(download_and_process, request.track_id, request.location, request.video_id)
    
    return {
        "status": "queued",
        "message": f"Download started to {location_msg}",
        "track_id": request.track_id
    }

@app.get("/api/download/status/{track_id}")
async def get_download_status(track_id: str):
    """Get download status for a track"""
    if track_id not in download_status:
        raise HTTPException(status_code=404, detail="Download not found")
    
    return download_status[track_id]

# Album download status storage
album_download_status: Dict[str, Dict] = {}

@app.post("/api/download/album")
async def download_album(request: AlbumDownloadRequest, background_tasks: BackgroundTasks):
    """Start downloading all tracks from an album"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    # Get album details
    album = spotify_service.get_album_details(request.album_id)
    if not album:
        raise HTTPException(status_code=404, detail="Album not found")
    
    # Validate location
    location = request.location if request.location in ["local", "navidrome"] else "local"
    location_msg = "local downloads folder" if location == "local" else "Navidrome server"
    
    # Initialize album status
    album_download_status[request.album_id] = {
        "status": "downloading",
        "album_name": album['name'],
        "artist": album['artist'],
        "total_tracks": len(album['tracks']),
        "completed_tracks": 0,
        "failed_tracks": 0,
        "current_track": None,
        "track_ids": [t['id'] for t in album['tracks']]
    }
    
    # Queue each track for download
    for track in album['tracks']:
        download_status[track['id']] = {
            "status": "queued",
            "message": f"Queued (Album: {album['name']})",
            "progress": 0,
            "stage": "queued",
            "album_id": request.album_id
        }
        background_tasks.add_task(download_album_track, track['id'], location, request.album_id)
    
    return {
        "status": "queued",
        "message": f"Album '{album['name']}' queued for download to {location_msg}",
        "album_id": request.album_id,
        "total_tracks": len(album['tracks'])
    }

def download_album_track(track_id: str, location: str, album_id: str):
    """Download a single track as part of an album download"""
    try:
        # Update album status
        if album_id in album_download_status:
            album_download_status[album_id]["current_track"] = track_id
        
        # Use existing download function
        download_and_process(track_id, location, None)
        
        # Update album completion status
        if album_id in album_download_status:
            status = download_status.get(track_id, {})
            if status.get("status") == "completed":
                album_download_status[album_id]["completed_tracks"] += 1
            else:
                album_download_status[album_id]["failed_tracks"] += 1
            
            # Check if album is complete
            total = album_download_status[album_id]["total_tracks"]
            completed = album_download_status[album_id]["completed_tracks"]
            failed = album_download_status[album_id]["failed_tracks"]
            
            if completed + failed >= total:
                album_download_status[album_id]["status"] = "completed"
                album_download_status[album_id]["current_track"] = None
    except Exception as e:
        if album_id in album_download_status:
            album_download_status[album_id]["failed_tracks"] += 1
        print(f"Error downloading album track {track_id}: {e}")

@app.get("/api/download/album/status/{album_id}")
async def get_album_download_status(album_id: str):
    """Get download status for an album"""
    if album_id not in album_download_status:
        raise HTTPException(status_code=404, detail="Album download not found")
    
    return album_download_status[album_id]

@app.get("/api/youtube/candidates/{track_id}")
async def get_youtube_candidates(track_id: str):
    """Get YouTube candidates for a track to let user choose if confidence is low"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        # Get track details from Spotify
        track_info = spotify_service.get_track_details(track_id)
        if not track_info:
            raise HTTPException(status_code=404, detail="Track not found")
        
        # Search YouTube for candidates
        result = youtube_service.search_candidates(
            track_info['name'],
            track_info['artist'],
            track_info
        )
        
        return {
            "track": {
                "id": track_id,
                "name": track_info['name'],
                "artist": track_info['artist'],
                "album": track_info.get('album', '')
            },
            **result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching YouTube: {str(e)}")

@app.get("/api/download/file/{track_id}")
async def download_file(track_id: str, filename: str = Query(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    """Download a file (for local browser downloads) and delete temp file afterward"""
    if track_id not in download_status:
        raise HTTPException(status_code=404, detail="Download not found")
    
    status = download_status[track_id]
    if status.get('status') != 'completed':
        raise HTTPException(status_code=400, detail="File not ready for download")
    
    file_path = status.get('file_path')
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    # Decode URL-encoded filename for comparison
    decoded_filename = unquote(filename)
    actual_filename = os.path.basename(file_path)
    
    # Verify filename matches for security (compare decoded vs actual)
    if actual_filename != decoded_filename:
        raise HTTPException(status_code=400, detail=f"Invalid filename. Expected: {actual_filename}, Got: {decoded_filename}")
    
    # Check if this is a temp file (for local downloads) - delete after serving
    # Normalize paths for comparison
    temp_dir_path = str(Path(config.DOWNLOAD_DIR) / "temp")
    is_temp_file = temp_dir_path in file_path or "temp" in os.path.dirname(file_path)
    
    # Return file for browser to download (saves to user's Downloads folder)
    # Use decoded filename for Content-Disposition header
    response = FileResponse(
        file_path,
        media_type='audio/mpeg',
        filename=decoded_filename,
        headers={"Content-Disposition": f"attachment; filename=\"{decoded_filename}\""}
    )
    
    # Delete temp file after download completes (only for local downloads)
    if is_temp_file:
        background_tasks.add_task(cleanup_temp_file, file_path)
    
    return response

def cleanup_temp_file(file_path: str):
    """Clean up temporary download file after it's been served"""
    try:
        # Wait a moment to ensure file transfer is complete
        time.sleep(2)
        if os.path.exists(file_path):
            os.remove(file_path)
            print(f"Cleaned up temp file: {file_path}")
    except Exception as e:
        print(f"Error cleaning up temp file {file_path}: {e}")

@app.get("/api/track/{track_id}/exists")
async def check_track_exists(track_id: str):
    """Check if a track file already exists in downloads"""
    if not spotify_service:
        raise HTTPException(status_code=500, detail="Spotify service not configured")
    
    try:
        # Get track details from Spotify
        track_info = spotify_service.get_track_details(track_id)
        if not track_info:
            return {"exists": False}
        
        # Check if file exists
        download_path = get_download_path(track_info, config.DOWNLOAD_DIR, config.OUTPUT_FORMAT)
        file_exists = os.path.exists(download_path)
        
        return {
            "exists": file_exists,
            "file_path": download_path if file_exists else None
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error checking track: {str(e)}")

@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "spotify_configured": spotify_service is not None,
        "navidrome_path": config.NAVIDROME_MUSIC_PATH
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)

