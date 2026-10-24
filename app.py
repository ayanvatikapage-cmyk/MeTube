import os
import glob
import uuid
import re
import asyncio
from typing import Optional
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="HyperDownloader")

DOWNLOAD_DIR = "/tmp/downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
COOKIES_FILE = "/app/cookies.txt"

def get_base_ydl_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'extract_flat': False,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'configs']
            }
        }
    }
    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
        opts['cookiefile'] = COOKIES_FILE
    return opts

def cleanup_file(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass

def format_duration(seconds: Optional[int]) -> str:
    if not seconds:
        return "Unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    return name.strip() or "video"

class InfoRequest(BaseModel):
    url: str

class CookieRequest(BaseModel):
    cookies: str

@app.post("/api/cookies")
async def save_cookies(req: CookieRequest):
    try:
        with open(COOKIES_FILE, "w", encoding="utf-8") as f:
            f.write(req.cookies.strip())
        return {"status": "success", "message": "Cookies saved successfully!"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/cookies/status")
async def cookies_status():
    exists = os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10
    return {"has_cookies": exists}

@app.post("/api/info")
async def get_video_info(req: InfoRequest):
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL cannot be empty")

    ydl_opts = get_base_ydl_opts()
    
    try:
        # Run extraction in threadpool so it doesn't block event loop
        loop = asyncio.get_event_loop()
        def extract():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await loop.run_in_executor(None, extract)
    except Exception as e:
        err_msg = str(e)
        if "Failed to extract any player response" in err_msg or "Sign in to confirm" in err_msg:
            raise HTTPException(
                status_code=400,
                detail="YouTube requires cookies for this video on cloud servers. Please paste your cookies using the 'YouTube Cookies' button below."
            )
        raise HTTPException(status_code=400, detail=f"Failed to fetch video: {err_msg}")

    if not info:
        raise HTTPException(status_code=404, detail="Video details could not be found.")

    extractor = info.get("extractor", "").lower()
    title = info.get("title", "Untitled Video")
    thumbnail = info.get("thumbnail") or ""
    duration = format_duration(info.get("duration"))
    uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown Creator"

    # Identify platform
    platform = "Generic"
    if "youtube" in extractor or "youtu.be" in url:
        platform = "YouTube"
    elif "tiktok" in extractor:
        platform = "TikTok"
    elif "facebook" in extractor:
        platform = "Facebook"
    elif "instagram" in extractor:
        platform = "Instagram"
    elif "twitter" in extractor or "x.com" in url:
        platform = "Twitter/X"

    raw_formats = info.get("formats", [])
    format_options = []

    if platform == "TikTok":
        # TikTok: Direct No-Watermark HD & Audio
        format_options.append({
            "label": "HD Video (No Watermark)",
            "quality": "Full HD",
            "ext": "mp4",
            "type": "video",
            "format_id": "best"
        })
        format_options.append({
            "label": "Original Audio",
            "quality": "MP3 Audio",
            "ext": "mp3",
            "type": "audio",
            "format_id": "bestaudio"
        })
    elif platform == "Facebook":
        # Facebook HD & SD
        format_options.append({
            "label": "HD Quality (1080p/720p)",
            "quality": "HD Video",
            "ext": "mp4",
            "type": "video",
            "format_id": "best[height>=720]/best"
        })
        format_options.append({
            "label": "SD Quality (480p/360p)",
            "quality": "SD Video",
            "ext": "mp4",
            "type": "video",
            "format_id": "worst[height>=360]/worst"
        })
        format_options.append({
            "label": "Audio Only",
            "quality": "MP3 Audio",
            "ext": "mp3",
            "type": "audio",
            "format_id": "bestaudio"
        })
    else:
        # YouTube / Instagram / Others
        # Extract available video resolutions
        heights = set()
        for f in raw_formats:
            h = f.get("height")
            if h and f.get("vcodec") != "none" and h in [1080, 720, 480, 360, 240, 144]:
                heights.add(h)
        
        sorted_heights = sorted(list(heights), reverse=True)
        if not sorted_heights:
            sorted_heights = [720, 360]

        for h in sorted_heights:
            label = f"{h}p Full HD" if h >= 1080 else (f"{h}p HD" if h >= 720 else f"{h}p SD")
            format_options.append({
                "label": label,
                "quality": f"{h}p",
                "ext": "mp4",
                "type": "video",
                "format_id": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
            })

        format_options.append({
            "label": "Audio (High Quality)",
            "quality": "MP3 320k",
            "ext": "mp3",
            "type": "audio",
            "format_id": "bestaudio"
        })

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "uploader": uploader,
        "platform": platform,
        "formats": format_options,
        "original_url": url
    }

@app.get("/api/download")
async def download_media(
    url: str = Query(...),
    format_id: str = Query(...),
    ext: str = Query("mp4"),
    title: str = Query("video"),
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    clean_title = sanitize_filename(title)
    uid = uuid.uuid4().hex[:8]
    output_template = os.path.join(DOWNLOAD_DIR, f"{uid}_%(title)s.%(ext)s")

    ydl_opts = {
        'format': format_id,
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'extractor_args': {
            'youtube': {
                'player_client': ['android', 'ios', 'web'],
                'player_skip': ['webpage', 'configs']
            }
        }
    }

    if os.path.exists(COOKIES_FILE) and os.path.getsize(COOKIES_FILE) > 10:
        ydl_opts['cookiefile'] = COOKIES_FILE

    if ext == "mp3":
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }]
    elif ext == "mp4":
        ydl_opts['merge_output_format'] = 'mp4'

    loop = asyncio.get_event_loop()
    def do_download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

    try:
        await loop.run_in_executor(None, do_download)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Download processing failed: {str(e)}")

    # Locate the downloaded file
    matching = glob.glob(os.path.join(DOWNLOAD_DIR, f"{uid}_*"))
    if not matching:
        raise HTTPException(status_code=404, detail="File could not be prepared.")

    filepath = matching[0]
    actual_ext = os.path.splitext(filepath)[1]
    final_filename = f"{clean_title}{actual_ext}"

    # Schedule cleanup after transmission
    background_tasks.add_task(cleanup_file, filepath)

    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    return FileResponse(
        path=filepath,
        filename=final_filename,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{final_filename}"'}
    )

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>UltraDownloader - High Speed Media Downloader</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    body { font-family: 'Plus Jakarta Sans', sans-serif; }
    .glass { background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(16px); border: 1px solid rgba(255, 255, 255, 0.08); }
    .glow { box-shadow: 0 0 40px -10px rgba(56, 189, 248, 0.3); }
    .btn-gradient { background: linear-gradient(135deg, #0ea5e9 0%, #3b82f6 50%, #6366f1 100%); }
    .btn-gradient:hover { background: linear-gradient(135deg, #38bdf8 0%, #2563eb 50%, #4f46e5 100%); }
    @keyframes pulse-slow { 0%, 100% { opacity: 0.2; transform: scale(1); } 50% { opacity: 0.35; transform: scale(1.05); } }
    .bg-glow { animation: pulse-slow 8s infinite ease-in-out; }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col relative overflow-x-hidden">

  <!-- Ambient Glow Background -->
  <div class="absolute -top-40 left-1/2 -translate-x-1/2 w-[700px] h-[400px] bg-sky-500/20 blur-[130px] rounded-full pointer-events-none bg-glow"></div>
  <div class="absolute top-96 -left-32 w-80 h-80 bg-indigo-500/15 blur-[120px] rounded-full pointer-events-none"></div>

  <!-- Header -->
  <header class="w-full border-b border-slate-800/80 glass sticky top-0 z-50">
    <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="w-10 h-10 rounded-xl bg-gradient-to-tr from-sky-500 to-indigo-600 flex items-center justify-center shadow-lg shadow-sky-500/20">
          <svg class="w-6 h-6 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M13 10V3L4 14h7v7l9-11h-7z"/>
          </svg>
        </div>
        <div>
          <span class="text-xl font-extrabold tracking-tight bg-gradient-to-r from-white via-sky-200 to-sky-400 bg-clip-text text-transparent">ULTRA<span class="text-sky-400 font-black">STREAM</span></span>
          <span class="hidden sm:inline-block text-xs font-semibold px-2 py-0.5 ml-2 rounded-full bg-sky-500/10 text-sky-400 border border-sky-500/20">Datacenter 1Gbps</span>
        </div>
      </div>
      <button onclick="toggleCookiesModal()" class="text-xs font-medium px-3 py-1.5 rounded-lg bg-slate-900 border border-slate-800 hover:border-slate-700 text-slate-300 hover:text-white transition flex items-center gap-1.5">
        <svg class="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"/></svg>
        YouTube Cookies
      </button>
    </div>
  </header>

  <!-- Main Container -->
  <main class="flex-1 max-w-4xl w-full mx-auto px-4 py-12 flex flex-col items-center">
    
    <!-- Hero Title -->
    <div class="text-center mb-8 max-w-2xl">
      <h1 class="text-3xl sm:text-5xl font-extrabold tracking-tight mb-4">
        High Speed Video <span class="bg-gradient-to-r from-sky-400 to-indigo-400 bg-clip-text text-transparent">Downloader</span>
      </h1>
      <p class="text-slate-400 text-sm sm:text-base leading-relaxed">
        Download videos directly in Full HD & 4K with maximum bandwidth. 100% private, zero ads, no third-party throttles.
      </p>

      <!-- Supported Badges -->
      <div class="flex flex-wrap items-center justify-center gap-2 mt-5">
        <span class="text-xs px-3 py-1 rounded-full bg-red-500/10 text-red-400 border border-red-500/20 font-medium">YouTube</span>
        <span class="text-xs px-3 py-1 rounded-full bg-teal-500/10 text-teal-400 border border-teal-500/20 font-medium">TikTok (No Watermark)</span>
        <span class="text-xs px-3 py-1 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20 font-medium">Facebook HD</span>
        <span class="text-xs px-3 py-1 rounded-full bg-pink-500/10 text-pink-400 border border-pink-500/20 font-medium">Instagram</span>
        <span class="text-xs px-3 py-1 rounded-full bg-slate-800 text-slate-300 border border-slate-700 font-medium">Twitter / X</span>
      </div>
    </div>

    <!-- Search / Input Card -->
    <div class="w-full glass rounded-2xl p-2.5 sm:p-3 glow shadow-2xl mb-8">
      <form id="fetchForm" onsubmit="handleFetch(event)" class="flex flex-col sm:flex-row gap-2">
        <div class="relative flex-1">
          <input 
            type="url" 
            id="urlInput"
            required
            placeholder="Paste YouTube, TikTok or Facebook video link here..." 
            class="w-full h-13 bg-slate-900/90 text-white placeholder-slate-500 px-4 py-3.5 pl-11 rounded-xl border border-slate-800 focus:outline-none focus:border-sky-500 transition text-sm sm:text-base"
          />
          <svg class="w-5 h-5 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"/>
          </svg>
          <button 
            type="button" 
            onclick="pasteClipboard()" 
            class="absolute right-3 top-1/2 -translate-y-1/2 text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 px-2 py-1 rounded-md transition"
          >
            Paste
          </button>
        </div>
        <button 
          type="submit" 
          id="fetchBtn"
          class="btn-gradient h-13 px-7 rounded-xl font-bold text-white shadow-lg shadow-sky-500/25 flex items-center justify-center gap-2 hover:opacity-95 transition cursor-pointer"
        >
          <span id="btnText">Fetch Video</span>
          <svg id="btnSpinner" class="hidden animate-spin w-5 h-5 text-white" fill="none" viewBox="0 0 24 24">
            <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
            <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path>
          </svg>
        </button>
      </form>
    </div>

    <!-- Error Alert -->
    <div id="errorAlert" class="hidden w-full p-4 mb-6 rounded-xl bg-red-950/40 border border-red-500/30 text-red-300 text-sm flex items-start gap-3">
      <svg class="w-5 h-5 text-red-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/></svg>
      <div id="errorMessage">An error occurred while fetching video.</div>
    </div>

    <!-- Video Preview & Resolutions Card -->
    <div id="resultCard" class="hidden w-full glass rounded-2xl p-6 glow">
      <div class="flex flex-col md:flex-row gap-6 items-start">
        <!-- Thumbnail -->
        <div class="w-full md:w-72 aspect-video bg-slate-900 rounded-xl overflow-hidden relative shadow-lg shrink-0 border border-slate-800">
          <img id="videoThumb" src="" alt="Thumbnail" class="w-full h-full object-cover"/>
          <span id="videoDuration" class="absolute bottom-2 right-2 bg-black/80 backdrop-blur-md text-[11px] font-bold px-2 py-0.5 rounded text-white">00:00</span>
          <span id="videoPlatformBadge" class="absolute top-2 left-2 text-[10px] uppercase tracking-wider font-extrabold px-2 py-0.5 rounded bg-sky-500 text-white">YouTube</span>
        </div>

        <!-- Video Metadata -->
        <div class="flex-1 w-full flex flex-col justify-between">
          <div>
            <h2 id="videoTitle" class="text-lg sm:text-xl font-bold text-white line-clamp-2 leading-snug mb-2">Video Title</h2>
            <p id="videoAuthor" class="text-xs sm:text-sm text-slate-400 mb-5">By Creator</p>
          </div>

          <!-- Formats / Resolutions Title -->
          <div class="border-t border-slate-800/80 pt-4">
            <h3 class="text-xs uppercase tracking-wider font-bold text-slate-400 mb-3 flex items-center gap-1.5">
              <svg class="w-4 h-4 text-sky-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
              Select Download Resolution
            </h3>

            <!-- Resolution Grid -->
            <div id="formatList" class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
              <!-- Dynamically Populated -->
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Download Progress Toast -->
    <div id="downloadToast" class="fixed bottom-6 right-6 hidden glass rounded-xl p-4 border border-sky-500/40 shadow-2xl flex items-center gap-3 z-50 animate-bounce">
      <div class="w-8 h-8 rounded-full bg-sky-500/20 text-sky-400 flex items-center justify-center shrink-0">
        <svg class="w-5 h-5 animate-spin" fill="none" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8z"></path></svg>
      </div>
      <div>
        <h4 class="text-sm font-bold text-white">Starting Download...</h4>
        <p class="text-xs text-slate-400">Your browser will download the file at maximum speed.</p>
      </div>
    </div>

  </main>

  <!-- Cookies Modal -->
  <div id="cookiesModal" class="fixed inset-0 bg-black/70 backdrop-blur-sm hidden z-50 flex items-center justify-center p-4">
    <div class="glass max-w-lg w-full rounded-2xl p-6 border border-slate-700">
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-bold text-white flex items-center gap-2">
          <span>🍪</span> YouTube Cookies (Bypass Bot Check)
        </h3>
        <button onclick="toggleCookiesModal()" class="text-slate-400 hover:text-white">&times;</button>
      </div>
      <p class="text-xs text-slate-400 mb-3">
        If YouTube blocks datacenter servers with bot warnings, export your browser's <code class="text-sky-300">cookies.txt</code> (using extensions like "Get cookies.txt LOCALLY") and paste the text below:
      </p>
      <textarea id="cookiesInput" rows="6" placeholder="# Netscape HTTP Cookie File&#10;.youtube.com..." class="w-full bg-slate-900 border border-slate-800 rounded-xl p-3 text-xs font-mono text-slate-300 focus:outline-none focus:border-sky-500 mb-4"></textarea>
      <div class="flex justify-end gap-2">
        <button onclick="toggleCookiesModal()" class="px-4 py-2 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-semibold text-slate-300">Cancel</button>
        <button onclick="saveCookies()" class="px-4 py-2 rounded-lg btn-gradient text-xs font-bold text-white">Save Cookies</button>
      </div>
    </div>
  </div>

  <!-- Footer -->
  <footer class="border-t border-slate-800/80 py-6 text-center text-xs text-slate-500">
    UltraStream Downloader &bull; Hosted on Private Render Singapore Datacenter &bull; Fast & Secure
  </footer>

  <script>
    let currentVideoData = null;

    async function pasteClipboard() {
      try {
        const text = await navigator.clipboard.readText();
        if (text) {
          document.getElementById('urlInput').value = text;
          handleFetch(new Event('submit'));
        }
      } catch (err) {
        alert("Clipboard permission denied. Please paste manually into the box.");
      }
    }

    async function handleFetch(e) {
      e.preventDefault();
      const url = document.getElementById('urlInput').value.trim();
      if (!url) return;

      const fetchBtn = document.getElementById('fetchBtn');
      const btnText = document.getElementById('btnText');
      const btnSpinner = document.getElementById('btnSpinner');
      const errorAlert = document.getElementById('errorAlert');
      const errorMessage = document.getElementById('errorMessage');
      const resultCard = document.getElementById('resultCard');

      // Reset states
      errorAlert.classList.add('hidden');
      resultCard.classList.add('hidden');
      btnText.textContent = 'Analyzing...';
      btnSpinner.classList.remove('hidden');
      fetchBtn.disabled = true;

      try {
        const res = await fetch('/api/info', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });

        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.detail || 'Failed to extract video information.');
        }

        currentVideoData = data;
        renderResult(data);
      } catch (err) {
        errorMessage.textContent = err.message;
        errorAlert.classList.remove('hidden');
      } finally {
        btnText.textContent = 'Fetch Video';
        btnSpinner.classList.add('hidden');
        fetchBtn.disabled = false;
      }
    }

    function renderResult(data) {
      document.getElementById('videoThumb').src = data.thumbnail || 'https://via.placeholder.com/640x360?text=No+Preview';
      document.getElementById('videoTitle').textContent = data.title;
      document.getElementById('videoAuthor').textContent = `Uploaded by ${data.uploader}`;
      document.getElementById('videoDuration').textContent = data.duration;
      document.getElementById('videoPlatformBadge').textContent = data.platform;

      const formatList = document.getElementById('formatList');
      formatList.innerHTML = '';

      data.formats.forEach(f => {
        const btn = document.createElement('a');
        btn.href = `/api/download?url=${encodeURIComponent(data.original_url)}&format_id=${encodeURIComponent(f.format_id)}&ext=${f.ext}&title=${encodeURIComponent(data.title)}`;
        btn.target = "_blank";
        btn.onclick = () => showToast();
        btn.className = "flex items-center justify-between p-3 rounded-xl bg-slate-900/80 hover:bg-slate-800 border border-slate-800 hover:border-sky-500/50 transition cursor-pointer group";

        const isAudio = f.type === 'audio';
        const iconSvg = isAudio 
          ? `<svg class="w-4 h-4 text-purple-400 group-hover:scale-110 transition" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19V6l12-3v13M9 19c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zm12-3c0 1.105-1.343 2-3 2s-3-.895-3-2 1.343-2 3-2 3 .895 3 2zM9 10l12-3"/></svg>`
          : `<svg class="w-4 h-4 text-sky-400 group-hover:scale-110 transition" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>`;

        btn.innerHTML = `
          <div class="flex items-center gap-2.5">
            ${iconSvg}
            <div class="text-left">
              <div class="text-xs font-bold text-white group-hover:text-sky-300 transition">${f.label}</div>
              <div class="text-[10px] text-slate-400 uppercase font-semibold">${f.quality} &bull; ${f.ext}</div>
            </div>
          </div>
          <span class="text-xs px-2.5 py-1 rounded-md bg-sky-500/10 text-sky-400 group-hover:bg-sky-500 group-hover:text-white font-bold transition flex items-center gap-1">
            Download
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M14 5l7 7m0 0l-7 7m7-7H3"/></svg>
          </span>
        `;
        formatList.appendChild(btn);
      });

      document.getElementById('resultCard').classList.remove('hidden');
    }

    function showToast() {
      const toast = document.getElementById('downloadToast');
      toast.classList.remove('hidden');
      setTimeout(() => {
        toast.classList.add('hidden');
      }, 5000);
    }

    function toggleCookiesModal() {
      document.getElementById('cookiesModal').classList.toggle('hidden');
    }

    async function saveCookies() {
      const cookies = document.getElementById('cookiesInput').value;
      if (!cookies.trim()) return alert("Please paste cookie data.");

      try {
        const res = await fetch('/api/cookies', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cookies })
        });
        const data = await res.json();
        alert(data.message || "Saved!");
        toggleCookiesModal();
      } catch (err) {
        alert("Failed to save cookies: " + err.message);
      }
    }
  </script>
</body>
</html>
"""
