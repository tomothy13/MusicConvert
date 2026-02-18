import os
import re
import argparse
import subprocess
import time
import glob
import yt_dlp
import queue
from logging_setup import setup_logging

# initialize logging for CLI/downloader
logger = setup_logging('musicconvert.main')


def download_url_to_m4a(url, output_dir='.', archive_file='archive.txt', error_file='error.txt', progress_queue: 'queue.Queue|None'=None):
    """Download a single video or playlist/album as AAC in an M4A container.

    Behavior:
    - Playlists/albums -> create sanitized folder named after playlist title.
    - Singles -> saved in `output_dir`.
    - Embeds metadata and cover art when FFmpeg available.
    """
    # First, probe the URL to see if it's a playlist
    probe_opts = {'quiet': True, 'skip_download': True}
    try:
        with yt_dlp.YoutubeDL(probe_opts) as probe_ydl:
            info = probe_ydl.extract_info(url, download=False)
    except Exception as e:
        print(f"Failed to read URL info: {e}")
        return

    is_playlist = info.get('_type') == 'playlist' or 'entries' in info

    if is_playlist:
        # Determine and sanitize playlist title, create folder up-front
        playlist_title = info.get('title') or info.get('playlist_title') or 'playlist'
        safe_title = sanitize_filename(playlist_title)
        album_dir = os.path.join(output_dir, safe_title)
        os.makedirs(album_dir, exist_ok=True)
        outtmpl = os.path.join(album_dir, '%(playlist_index)s - %(title)s.%(ext)s')
    else:
        os.makedirs(output_dir, exist_ok=True)
        outtmpl = os.path.join(output_dir, '%(title)s.%(ext)s')

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'm4a',
            'preferredquality': '256',
        }],
        # Embed metadata and thumbnails where possible (FFmpeg used under the hood)
        'writethumbnail': True,
        'embedthumbnail': True,
        'addmetadata': True,
        'prefer_ffmpeg': True,
        # Ensure MP4 files are optimized for players (faststart) and set reasonable defaults
        'postprocessor_args': ['-movflags', '+faststart'],
        'outtmpl': outtmpl,
        'ignoreerrors': True,
        'quiet': False,
        'noplaylist': False,
    }

    # If a thread-safe progress queue is provided, attach a yt-dlp progress hook
    if progress_queue is not None:
        def _make_hook(q):
            def _hook(d):
                try:
                    status = d.get('status')
                    if status == 'downloading':
                        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
                        downloaded = d.get('downloaded_bytes', 0)
                        speed = d.get('speed', 0) or 0
                        eta = d.get('eta')
                        pct = (downloaded / total * 100.0) if total else 0.0
                        q.put(f"downloading:{pct:.1f}%:{downloaded}:{speed}:{eta}")
                    elif status == 'finished':
                        q.put(f"finished:{d.get('filename','')}")
                    else:
                        q.put(f"status:{status}")
                except Exception:
                    pass
            return _hook

        ydl_opts['progress_hooks'] = [_make_hook(progress_queue)]

    logger.info("Starting download: %s -> %s", 'playlist' if is_playlist else 'video', output_dir)
    start_ts = time.time()
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        logger.info("Download finished for URL: %s", url)
        # Record the processed link to the archive with the album name
        if is_playlist:
            album_name = playlist_title
        else:
            album_name = info.get('title') or 'video'

        try:
            with open(archive_file, 'a', encoding='utf-8') as af:
                af.write(f"{album_name}\t{url}\n")
        except Exception as e:
            logger.warning("Failed to write to archive file %s: %s", archive_file, e)

        # Remove any thumbnails created during this download (webp/jpg/png)
        try:
            target_dir = album_dir if is_playlist else output_dir
            exts = ['.webp', '.jpg', '.jpeg', '.png']
            for ext in exts:
                for thumb in glob.glob(os.path.join(target_dir, f'*{ext}')):
                    try:
                        if os.path.getmtime(thumb) >= start_ts - 1:
                            os.remove(thumb)
                    except Exception:
                        pass
        except Exception:
            pass

        return True
    except Exception as e:
        logger.exception('An error occurred during download: %s', e)
        # Log to error file with album/title and the error message
        album_name = (playlist_title if is_playlist else (info.get('title') or 'video'))
        try:
            with open(error_file, 'a', encoding='utf-8') as ef:
                ef.write(f"{album_name}\t{url}\t{e}\n")
        except Exception as ee:
            logger.warning('Failed to write to error file %s: %s', error_file, ee)
        return False
    finally:
        # signal progress completion if a queue was provided
        try:
            if progress_queue is not None:
                progress_queue.put('__DL_DONE__')
        except Exception:
            pass


def sanitize_filename(name: str) -> str:
    """Return a filesystem-safe version of `name`."""
    # Replace path separators and control characters
    name = name.strip()
    name = re.sub(r'[\\/]+', '-', name)
    # Remove characters commonly unsafe for filenames
    name = re.sub(r'[:*?"<>|\x00-\x1f]', '', name)
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name)
    return name


def main():
    # ----------------------
    # CLI / Defaults
    # ----------------------
    DEFAULT_LINKS_FILE = 'links.txt'  # path to a text file with one URL per line
    DEFAULT_OUTPUT_DIR = '/Users/tomothy/Documents/MusicLibrary'

    parser = argparse.ArgumentParser(description='Download YouTube videos or playlists as MP3(s)')
    parser.add_argument('-u', '--url', help='Single YouTube/video/playlist URL to download')
    parser.add_argument('-l', '--links', help='Path to text file with links (one per line)')
    parser.add_argument('-o', '--output', help='Output directory (overrides script default)')
    args = parser.parse_args()

    links_file = args.links or DEFAULT_LINKS_FILE
    output_dir = args.output or DEFAULT_OUTPUT_DIR

    os.makedirs(output_dir, exist_ok=True)

    # Place archive and error files inside the output directory
    archive_file = os.path.join(output_dir, 'archive.txt')
    error_file = os.path.join(output_dir, 'error.txt')

    if args.url:
        download_url_to_m4a(args.url, output_dir, archive_file=archive_file, error_file=error_file)
        return

    # ----------------------
    # Links editing helper
    # Opens the links file in $EDITOR or macOS default and waits for save/close.
    def open_links_in_editor(path):
        # Ensure parent folder exists
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8') as wf:
                wf.write('# Paste links here, separated by commas or new lines.\n')

        editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
        if editor:
            try:
                subprocess.call([editor, path])
                return
            except Exception:
                pass

        # Fallback for macOS: open and wait until the app quits
        try:
            print(f"Opening {path} with default application. Save and close the file to continue.")
            subprocess.call(['open', '-W', path])
            return
        except Exception:
            pass

        # Final fallback: prompt user to edit manually
        print(f"Please edit the links file now: {path}\nPaste links, save and close the file, then press Enter to continue.")
        input()

    open_links_in_editor(links_file)

    if not os.path.exists(links_file):
        print(f"Links file not found after editor closed: {links_file}")
        return

    with open(links_file, 'r', encoding='utf-8') as f:
        contents = f.read()
        original_lines = f.readlines()

    # Split by commas and newlines, strip whitespace, ignore comments
    raw_links = re.split(r'[\n,]+', contents)
    candidate_links = [ln.strip() for ln in raw_links if ln.strip() and not ln.strip().startswith('#')]

    # Validate links: only accept http/https URLs
    from urllib.parse import urlparse
    links = []
    for ln in candidate_links:
        parsed = urlparse(ln)
        if parsed.scheme in ('http', 'https'):
            links.append(ln)
        else:
            print(f"Skipping invalid URL-like entry: {ln}")

    if not links:
        print("No valid links found in links file.")
        return

    # Process links one-by-one and remove successful ones from the links file
    processed_success = []
    for link in links:
        ok = download_url_to_m4a(link, output_dir, archive_file=archive_file, error_file=error_file)
        if ok:
            processed_success.append(link)
            # rewrite links_file with remaining links
            remaining = [l for l in links if l not in processed_success]
            try:
                with open(links_file, 'w', encoding='utf-8') as wf:
                    wf.write('# Paste links here, separated by commas or new lines.\n')
                    for r in remaining:
                        wf.write(r + '\n')
            except Exception as e:
                print(f"Warning: failed to update links file {links_file}: {e}")


if __name__ == '__main__':
    main()
