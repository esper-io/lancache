#!/usr/bin/env python3
"""
Esper EdgeBox

__version__: service version; bump this on each release (e.g. "1.0.0" → "1.0.1").
"""
__version__ = "1.0.0"

import os
import sys
import time
import json
import atexit
import shutil
import socket
import tempfile
import threading
import logging
import concurrent.futures
import urllib.parse
import random
import warnings

from http.client import HTTPConnection
from flask import Flask, jsonify, request, send_file
from flask_apscheduler import APScheduler
from zeroconf import ServiceInfo, Zeroconf
import crcmod
import requests
from requests.adapters import HTTPAdapter
from logging.handlers import RotatingFileHandler

# ------------------------------------------------------------------------------
# Logging Setup and Verbosity Toggle
# ------------------------------------------------------------------------------
VERBOSE = os.environ.get('VERBOSE_LOGGING', '0').lower() in ('1', 'true', 'yes')
SERVICE_NAME = "esper-edgebox"

logger = logging.getLogger(SERVICE_NAME)
logger.setLevel(logging.DEBUG if VERBOSE else logging.INFO)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(thread)d - %(message)s")
rotating_handler = RotatingFileHandler("esper_edgebox.log", maxBytes=10 * 1024 * 1024, backupCount=5)
rotating_handler.setFormatter(formatter)
logger.addHandler(rotating_handler)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

# ------------------------------------------------------------------------------
# Suppress InsecureRequestWarning
# ------------------------------------------------------------------------------
from requests.packages.urllib3.exceptions import InsecureRequestWarning
warnings.filterwarnings("ignore", category=InsecureRequestWarning)

# ------------------------------------------------------------------------------
# Global Configuration Variables
# ------------------------------------------------------------------------------
PORT = 8020             # Flask app port
TCP_PORT = 8021         # TCP server port for ping
MAX_CACHE_AGE = 60 * 60 * 24 * 20  # 20 days in seconds
MAX_CRC_AGE = 60 * 60   # 1 hour in seconds
ENABLE_CRC_CHECK = True
DOWNLOAD_TIMEOUT = 300  # 5 minutes timeout
DOWNLOAD_RETRIES = 3
CHUNK_SIZE = 512 * 1024 # 512KB chunk size
MAX_WORKERS = 6         # Parallel download threads
PARTIAL_DOWNLOAD = True  # Use Range requests when applicable

HTTPConnection.default_socket_options = [
    (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
    (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
    (socket.IPPROTO_TCP, socket.TCP_FASTOPEN, 1)
]
socket.SO_RCVBUF = 1024 * 1024 * 4  # 4MB receive buffer

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/83.0.4103.61 Safari/537.36")
}

# Initialize CRC function
try:
    crc64 = crcmod.predefined.mkPredefinedCrcFun('crc-64')
except KeyError:
    crc64 = crcmod.mkCrcFun(
        initCrc=0xFFFFFFFFFFFFFFFF,
        rev=True,
        xorOut=0xFFFFFFFFFFFFFFFF,
        poly=0x42F0E1EBA9EA3693
    )

NETWORK_CACHE = {}
REMOTE_CRC = {}
crcQueue = []
executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# ------------------------------------------------------------------------------
# Flask App and Scheduler
# ------------------------------------------------------------------------------
app = Flask(__name__)
scheduler = APScheduler()

# ------------------------------------------------------------------------------
# Cache and File Helper Functions
# ------------------------------------------------------------------------------
def getCacheDirPath() -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "cache")

def sanitize_file_name(file_name: str) -> str:
    logger.debug(f"sanitize_file_name: Original file name: {file_name}")
    file_name = file_name.encode("utf-8").decode("utf-8", "ignore")
    for char in '<>:"/\\|?':
        file_name = file_name.replace(char, "")
    sanitized = file_name.replace("+", " ")
    logger.debug(f"sanitize_file_name: Sanitized file name: {sanitized}")
    return sanitized

def getFileNameFromLink(link: str) -> str:
    logger.debug(f"getFileNameFromLink: link: {link}")
    parsed = urllib.parse.urlparse(link)
    file_name = sanitize_file_name(os.path.basename(parsed.path))
    logger.debug(f"getFileNameFromLink: file_name: {file_name}")
    return file_name

def getFilePathFromLink(link: str) -> str:
    file_name = getFileNameFromLink(link)
    file_path = os.path.join(getCacheDirPath(), file_name)
    logger.debug(f"getFilePathFromLink: file_path: {file_path}")
    return file_path

# ------------------------------------------------------------------------------
# CRC Calculation Helpers
# ------------------------------------------------------------------------------
def compute_crc64(path: str) -> str:
    crc_value = 0
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                crc_value = crc64(chunk, crc_value)
        return f"{crc_value:016x}"
    except Exception as e:
        logger.exception(f"compute_crc64: Error processing file {path}: {e}")
        return None

def getFileCrc64(path: str) -> str:
    logger.debug(f"getFileCrc64: Called for {path}")
    if not ENABLE_CRC_CHECK or not os.path.exists(path):
        return None
    file_name = os.path.basename(path)
    if file_name in NETWORK_CACHE and "crc64" in NETWORK_CACHE[file_name]:
        return NETWORK_CACHE[file_name]["crc64"]
    future = executor.submit(compute_crc64, path)
    crc_val = future.result()
    NETWORK_CACHE.setdefault(file_name, {})["crc64"] = crc_val
    logger.debug(f"getFileCrc64: Calculated CRC64 for {file_name}: {crc_val}")
    return crc_val

def getUrlCrc64Helper(url: str, file: str) -> str:
    logger.debug(f"getUrlCrc64Helper: Called for URL: {url}")
    time.sleep(random.uniform(0, 0.5))
    while file in crcQueue:
        time.sleep(0.1)
    if file not in REMOTE_CRC:
        crcQueue.append(file)
        try:
            crc_value = 0
            with requests.get(url, stream=True, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, verify=False) as r:
                r.raise_for_status()
                for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                    if chunk:
                        crc_value = crc64(chunk, crc_value)
            return f"{crc_value:016x}"
        except Exception as e:
            logger.exception(f"getUrlCrc64Helper: Error for URL {url}: {e}")
            return None
        finally:
            crcQueue.remove(file)
    else:
        return REMOTE_CRC[file]["crc64"]

def getCrc64FromUrl(url: str) -> str:
    logger.debug(f"getCrc64FromUrl: Called for {url}")
    if not ENABLE_CRC_CHECK:
        return None
    file = getFileNameFromLink(url)
    current_time = int(time.time())
    if file not in REMOTE_CRC or (current_time - REMOTE_CRC[file]["age"] > MAX_CRC_AGE):
        logger.info(f"getCrc64FromUrl: Checking remote CRC64 for {url}")
        remote_crc = getUrlCrc64Helper(url, file)
        REMOTE_CRC[file] = {"crc64": remote_crc, "age": current_time}
    return REMOTE_CRC[file]["crc64"]

def compareLocalRemoteCrc64(path: str, url: str) -> bool:
    logger.debug(f"compareLocalRemoteCrc64: path {path}, url {url}")
    if not ENABLE_CRC_CHECK:
        return True
    start = time.time()
    fileCrc = getFileCrc64(path) if os.path.exists(path) else None
    urlCrc = getCrc64FromUrl(url)
    elapsed = time.time() - start
    logger.debug(f"CRC compare: local: {fileCrc}, remote: {urlCrc} ({elapsed:.2f} sec)")
    return urlCrc == fileCrc

# ------------------------------------------------------------------------------
# Decorator: cache_download
# ------------------------------------------------------------------------------
def cache_download(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        link = get_full_link()
        if not link:
            logger.error("cache_download: No 'link' provided!")
            return jsonify({"error": "Missing link parameter"}), 400

        file_path = getFilePathFromLink(link)
        os.makedirs(getCacheDirPath(), exist_ok=True)
        file_name = getFileNameFromLink(link)

        # HIT: still fresh and CRC matches
        if (
            file_name in NETWORK_CACHE
            and os.path.exists(file_path)
            and (time.time() - NETWORK_CACHE[file_name].get("age", 0) < MAX_CACHE_AGE)
            and compareLocalRemoteCrc64(file_path, link)
        ):
            elapsed = time.time() - start_time
            logger.info(f"cache_download: Retrieved '{file_name}' from cache in {elapsed:.2f} sec")
            return send_file(NETWORK_CACHE[file_name]["result"], as_attachment=True)

        # MISS: download & then record CRCs
        logger.info("cache_download: Cache miss or CRC mismatch; downloading...")
        result = func(*args, **kwargs)

        # 1) update the in‑memory cache entry
        local_crc = getFileCrc64(file_path)
        NETWORK_CACHE.setdefault(file_name, {})["crc64"] = local_crc
        NETWORK_CACHE[file_name].update({
            "age": int(time.time()),
            "result": file_path
        })

        # 2) compute & record remote CRC (so next time compareLocalRemoteCrc64 is a simple dict lookup)
        try:
            remote_crc = getCrc64FromUrl(link)
            REMOTE_CRC[file_name] = {
                "crc64": remote_crc,
                "age": int(time.time())
            }
        except Exception as e:
            logger.warning(f"cache_download: Failed to compute remote CRC: {e}")

        # 3) persist both cache.json and remote_crc.json to disk immediately
        save_cache_to_file()
        save_remote_crc()

        elapsed = time.time() - start_time
        logger.info(f"cache_download: Download + CRC recorded in {elapsed:.2f} sec")
        return result

    return wrapper

def get_full_link():
    logger.debug("get_full_link: Called")
    base_link = request.args.get("link", "")
    if not base_link:
        logger.error("get_full_link: 'link' parameter missing!")
        return ""
    extra_params = {k: v for k, v in request.args.items() if k != "link"}
    if extra_params:
        extra_query = urllib.parse.urlencode(extra_params)
        separator = "&" if "?" in base_link else "?"
        full_link = base_link + separator + extra_query
        logger.info(f"get_full_link: Reconstructed link: {full_link}")
        return full_link
    logger.info(f"get_full_link: Link without extra params: {base_link}")
    return base_link

# ------------------------------------------------------------------------------
# Download Functions with .tmp Handling and Retry Logic
# ------------------------------------------------------------------------------
def simple_download(url: str, file_path: str) -> str:
    logger.info("simple_download: Starting single-threaded download")
    temp_path = file_path + ".tmp"
    bytes_downloaded = 0
    expected_size = None

    try:
        with requests.Session() as session:
            with session.get(url, stream=True, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, verify=False) as response:
                response.raise_for_status()
                # Try to get the expected file size from headers (if available)
                content_length = response.headers.get('Content-Length')
                if content_length:
                    expected_size = int(content_length)
                    logger.info(f"simple_download: Expected file size: {expected_size} bytes")
                else:
                    logger.info("simple_download: No Content-Length header found.")
                
                ensure_dir = os.path.dirname(file_path)
                if ensure_dir:
                    os.makedirs(ensure_dir, exist_ok=True)
                
                start_time = time.time()
                last_log_time = start_time
                with open(temp_path, 'wb') as f:
                    for i, chunk in enumerate(response.iter_content(chunk_size=CHUNK_SIZE)):
                        if chunk:
                            f.write(chunk)
                            bytes_downloaded += len(chunk)
                            now = time.time()
                            # Log progress every second (or on the first few chunks)
                            if now - last_log_time >= 1 or i < 5:
                                elapsed = now - start_time
                                mbps = (bytes_downloaded / (1024 * 1024)) / elapsed if elapsed > 0 else 0
                                logger.info(f"simple_download: Chunk {i}, total bytes downloaded: {bytes_downloaded} in {elapsed:.2f} s ({mbps:.2f} MB/s)")
                                last_log_time = now

        # Final verification if Content-Length was provided
        if expected_size is not None and bytes_downloaded < expected_size:
            raise Exception(f"Download incomplete: expected {expected_size} bytes, got {bytes_downloaded} bytes")

        os.replace(temp_path, file_path)
        os.chmod(file_path, 0o644)
        logger.info(f"simple_download: Download completed, total {bytes_downloaded} bytes received")
        return file_path

    except Exception as e:
        logger.error(f"simple_download: Download failed: {e}")
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise

def parallel_download(url: str, file_path: str, file_size: int) -> str:
    logger.info(f"parallel_download: Using multi-threaded download for {file_size} bytes")
    temp_path = file_path + ".tmp"
    num_threads = MAX_WORKERS
    chunk_size = file_size // num_threads
    ranges = [(i * chunk_size, (i + 1) * chunk_size - 1) for i in range(num_threads)]
    ranges[-1] = (ranges[-1][0], file_size - 1)
    temp_dir = tempfile.mkdtemp(dir=getCacheDirPath())
    def download_chunk(start, end, part_file):
        try:
            headers = {**HEADERS, "Range": f"bytes={start}-{end}"}
            with requests.Session() as session:
                session.mount('https://', HTTPAdapter(pool_maxsize=MAX_WORKERS))
                with session.get(url, headers=headers, stream=True, timeout=DOWNLOAD_TIMEOUT, verify=False) as response:
                    response.raise_for_status()
                    with open(part_file, 'wb') as pf:
                        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                            if chunk:
                                pf.write(chunk)
        except Exception as e:
            logger.error(f"parallel_download: Error downloading chunk {start}-{end}: {e}")
            raise
    futures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        for i, (start, end) in enumerate(ranges):
            part_file = os.path.join(temp_dir, f"part{i}")
            futures.append(executor.submit(download_chunk, start, end, part_file))
        for future in concurrent.futures.as_completed(futures):
            if future.exception():
                logger.error(f"parallel_download: A chunk failed: {future.exception()}")
                raise future.exception()
    with open(temp_path, 'wb') as outfile:
        for i in range(num_threads):
            part_path = os.path.join(temp_dir, f"part{i}")
            with open(part_path, 'rb') as infile:
                shutil.copyfileobj(infile, outfile, 1024 * 1024)
            os.remove(part_path)
    os.rmdir(temp_dir)
    os.replace(temp_path, file_path)
    os.chmod(file_path, 0o644)
    logger.info(f"parallel_download: Completed download of {os.path.getsize(file_path)} bytes")
    return file_path

def download_with_retry(url: str, file_path: str) -> str:
    attempt = 0
    backoff = 1
    while attempt < DOWNLOAD_RETRIES:
        attempt += 1
        try:
            start_time = time.time()
            if 'Signature=' in url and 'Expires=' in url:
                logger.info("Signed URL detected; skipping HEAD request.")
                use_parallel = False
                file_size = None
            else:
                use_parallel = False
                file_size = 0
                try:
                    head_resp = requests.head(url, headers=HEADERS, timeout=10, allow_redirects=True, verify=False)
                    head_resp.raise_for_status()
                    file_size = int(head_resp.headers.get('Content-Length', 0))
                    accept_ranges = head_resp.headers.get('Accept-Ranges', 'none')
                    if PARTIAL_DOWNLOAD and file_size > 10 * 1024 * 1024 and 'bytes' in accept_ranges.lower():
                        use_parallel = True
                except Exception as e:
                    logger.info(f"download_with_retry: HEAD request failed: {e}")
            download_func = parallel_download if (use_parallel and file_size) else simple_download
            download_func(url, file_path)
            elapsed = time.time() - start_time
            size = os.path.getsize(file_path)
            speed_mbps = (size / (1024 * 1024)) / elapsed if elapsed > 0 else 0
            logger.info(f"download_with_retry: Downloaded {os.path.basename(file_path)} ({size} bytes) in {elapsed:.2f}s at {speed_mbps:.2f} MB/s")
            if speed_mbps < 1.0:
                logger.warning("download_with_retry: Download speed is below 1 MB/s")
            return file_path
        except Exception as e:
            logger.error(f"download_with_retry: Attempt {attempt} failed: {e}")
            time.sleep(backoff)
            backoff *= 2
    raise Exception(f"download_with_retry: All {DOWNLOAD_RETRIES} attempts failed for {url}")

def downloadAsset(link: str) -> str:
    file_path = getFilePathFromLink(link)
    logger.info(f"downloadAsset: Initiating download for {link}")
    if 'Signature=' in link and 'Expires=' in link:
        logger.info("downloadAsset: Bypassing HEAD request for signed URL")
        return download_with_retry(link, file_path)
    if os.path.exists(file_path) and compareLocalRemoteCrc64(file_path, link):
        logger.info("downloadAsset: Valid cached file found, skipping download")
        return file_path
    return download_with_retry(link, file_path)

# ------------------------------------------------------------------------------
# Cache Persistence Helpers
# ------------------------------------------------------------------------------
def save_cache_to_file():
    logger.info("Saving cache references...")
    cache_dir = getCacheDirPath()
    os.makedirs(cache_dir, exist_ok=True)
    json_path = os.path.join(cache_dir, "cache.json")
    try:
        with open(json_path, "w") as f:
            json.dump(NETWORK_CACHE, f)
        logger.info("Cache saved successfully.")
    except Exception as e:
        logger.exception("Error saving cache file")

def read_saved_cache():
    global NETWORK_CACHE
    logger.info("Reading saved cache...")
    cache_dir = getCacheDirPath()
    json_path = os.path.join(cache_dir, "cache.json")
    os.makedirs(cache_dir, exist_ok=True)
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                NETWORK_CACHE = json.load(f)
            logger.info("Cache loaded successfully.")
        except Exception as e:
            logger.exception("Error reading cache file")
            NETWORK_CACHE = {}
    else:
        logger.debug("Cache file does not exist.")
        NETWORK_CACHE = {}

def read_remote_crc():
    global REMOTE_CRC
    logger.info("Reading remote CRC cache...")
    cache_dir = getCacheDirPath()
    json_path = os.path.join(cache_dir, "remote_crc.json")
    os.makedirs(cache_dir, exist_ok=True)
    if os.path.exists(json_path):
        try:
            with open(json_path, "r") as f:
                REMOTE_CRC = json.load(f)
            logger.info("Remote CRC cache loaded successfully.")
        except Exception as e:
            logger.exception("Error reading remote CRC file")
            REMOTE_CRC = {}
    else:
        logger.debug("Remote CRC cache file does not exist.")
        REMOTE_CRC = {}

def save_remote_crc():
    logger.info("Saving remote CRC cache...")
    cache_dir = getCacheDirPath()
    os.makedirs(cache_dir, exist_ok=True)
    json_path = os.path.join(cache_dir, "remote_crc.json")
    try:
        with open(json_path, "w") as f:
            json.dump(REMOTE_CRC, f)
        logger.info("Remote CRC cache saved successfully.")
    except Exception as e:
        logger.exception("Error saving remote CRC file")

def prepare_saved_cache():
    global NETWORK_CACHE
    logger.info("Preparing saved cache...")
    cache_dir = getCacheDirPath()
    read_saved_cache()
    if os.path.exists(cache_dir) and not NETWORK_CACHE:
        logger.info("No cache loaded, scanning cache directory.")
        for file in os.listdir(cache_dir):
            if file in ("cache.json", "remote_crc.json"):
                continue
            file_path = os.path.join(cache_dir, file)
            try:
                NETWORK_CACHE[file] = {
                    "age": int(time.time()),
                    "result": file_path,
                    "crc64": getFileCrc64(file_path)
                }
                logger.debug(f"Cached file {file}")
            except Exception as e:
                logger.exception(f"Error caching file {file}")
        save_cache_to_file()

def clear_aged_cache():
    logger.info("Clearing aged cache...")
    cache_dir = getCacheDirPath()
    for key in list(NETWORK_CACHE.keys()):
        if int(time.time()) - NETWORK_CACHE[key]["age"] > MAX_CACHE_AGE:
            logger.info(f"Clearing cache for: {key}")
            del NETWORK_CACHE[key]
            file_path = os.path.join(cache_dir, key)
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Removed file: {file_path}")
                except Exception as e:
                    logger.exception(f"Error removing file {file_path}")
    for key in list(REMOTE_CRC.keys()):
        if int(time.time()) - REMOTE_CRC[key]["age"] > MAX_CRC_AGE:
            logger.info(f"Clearing remote CRC for: {key}")
            del REMOTE_CRC[key]
    REMOTE_CRC.clear()

def saveJsonFiles():
    logger.info("Saving JSON files for cache and remote CRC.")
    save_cache_to_file()
    save_remote_crc()

# ------------------------------------------------------------------------------
# TCP Server (for PING)
# ------------------------------------------------------------------------------
def start_tcp_server(host: str = "0.0.0.0", port: int = None):
    logger.info("Starting TCP server...")
    if port is None:
        port = TCP_PORT
    def handle_client_connection(client_socket: socket.socket, client_address):
        logger.info(f"TCP Server: Connection from {client_address}")
        try:
            data = client_socket.recv(1024).decode("utf-8").strip()
            logger.info(f"TCP Server: Received data: '{data}'")
            if data == "PING":
                client_socket.sendall("PONG\n".encode("utf-8"))
                logger.info("TCP Server: Sent 'PONG' response")
            else:
                logger.info("TCP Server: Unrecognized command")
                client_socket.sendall("ERROR: Unrecognized command\n".encode("utf-8"))
        except Exception as e:
            logger.exception(f"TCP Server: Error with client {client_address}")
        finally:
            client_socket.close()
            logger.info(f"TCP Server: Closed connection from {client_address}")
    def tcp_server():
        logger.info("TCP Server: Main thread starting.")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(5)
        server.settimeout(1.0)
        logger.info(f"TCP Server: Listening on {host}:{port}")
        try:
            while not shutdown_event.is_set():
                try:
                    client_sock, client_addr = server.accept()
                    client_handler = threading.Thread(target=handle_client_connection, args=(client_sock, client_addr))
                    client_handler.daemon = True
                    client_handler.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.exception("TCP Server: Error accepting connection")
        except Exception as e:
            logger.exception("TCP Server: Unexpected error")
        finally:
            server.close()
            logger.info("TCP Server: Shutdown complete.")
    tcp_server_thread = threading.Thread(target=tcp_server)
    tcp_server_thread.daemon = True
    tcp_server_thread.start()

shutdown_event = threading.Event()

def teardown(*args, **kwargs):
    logger.info("Initiating teardown.")
    shutdown_event.set()
    saveJsonFiles()
    logger.info("Teardown complete.")

# ------------------------------------------------------------------------------
# Network Discovery via Zeroconf
# ------------------------------------------------------------------------------
class NetworkDiscoverySdk:
    def __init__(self, service_name: str, service_port: int):
        logger.debug("NetworkDiscoverySdk.__init__ called")
        self.service_name = service_name
        self.service_port = service_port
        self.zeroconf = Zeroconf()
        try:
            import subprocess
            hostname = subprocess.check_output(["hostnamectl", "--static"]).decode("utf-8").strip()
        except Exception as e:
            logger.error(f"Error obtaining hostname: {e}")
            hostname = socket.gethostname()
        if not hostname.endswith('.local'):
            hostname += '.local'
        self.hostname = hostname
        logger.info(f"Advertised hostname: {self.hostname}")

    def register(self):
        logger.debug("NetworkDiscoverySdk.register called")
        hostname_property = self.hostname.rstrip('.')
        properties = {
            "tcp_port": str(TCP_PORT).encode("utf-8"),
            "hostname": hostname_property.encode("utf-8"),
            "description": f"{SERVICE_NAME} service".encode("utf-8")
        }
        server_hostname = self.hostname if self.hostname.endswith('.') else self.hostname + '.'
        service_info = ServiceInfo(
            "_http._tcp.local.",
            f"{self.service_name}._http._tcp.local.",
            addresses=[socket.inet_aton(self.__get_ip())],
            port=self.service_port,
            properties=properties,
            server=server_hostname
        )
        self.zeroconf.register_service(service_info)
        logger.info(f"Service registered at {hostname_property}:{self.service_port}")

    def unregister(self):
        logger.debug("NetworkDiscoverySdk.unregister called")
        self.zeroconf.unregister_all_services()
        self.zeroconf.close()
        logger.info("Service unregistered successfully.")

    def __get_ip(self):
        logger.debug("Getting IP via __get_ip")
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        try:
            s.connect(('10.254.254.254', 1))
            IP = s.getsockname()[0]
            logger.debug(f"Obtained IP: {IP}")
        except Exception as e:
            logger.error(f"Error obtaining IP: {e}")
            IP = '127.0.0.1'
        finally:
            s.close()
        return IP

# ------------------------------------------------------------------------------
# Flask Routes
# ------------------------------------------------------------------------------
@app.route("/", methods=["GET"])
def root():
    logger.info("root: Request received")
    return jsonify({"message": "Esper EdgeBox Server"})

@app.route("/clear_cache", methods=["GET"])
def clear_cache():
    logger.info("clear_cache: Clearing cache...")
    NETWORK_CACHE.clear()
    cache_dir = getCacheDirPath()
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            logger.info("Cache directory removed.")
        except Exception as e:
            logger.exception("Error removing cache directory")
    else:
        logger.debug("Cache directory does not exist.")
    return jsonify({"message": "Cache cleared."})

@app.route("/download/", methods=["GET", "HEAD"])
@cache_download
def download():
    logger.info("/download: Endpoint called (method: %s)", request.method)
    link = get_full_link()
    if not link:
        logger.error("/download: Missing link parameter!")
        return jsonify({"error": "Missing link parameter"}), 400
    file_path = getFilePathFromLink(link)
    os.makedirs(getCacheDirPath(), exist_ok=True)
    logger.debug(f"/download: File path: {file_path}")
    if (not os.path.exists(file_path) or
        getFileNameFromLink(link) not in NETWORK_CACHE or
        not compareLocalRemoteCrc64(file_path, link)):
        logger.info("/download: File not cached or outdated; downloading.")
        try:
            downloadAsset(link)
        except Exception as e:
            logger.exception("/download: downloadAsset failed")
            return jsonify({"error": f"Download failed: {e}"}), 500
        # Wait for up to 10 seconds to ensure file exists (helps with HEAD requests)
        wait_time = 0.0
        while not os.path.exists(file_path) and wait_time < 10:
            time.sleep(0.5)
            wait_time += 0.5
        if not os.path.exists(file_path):
            logger.error("/download: File still not found after download attempt.")
            return jsonify({"error": "File download did not complete in time"}), 500
    else:
        logger.info("/download: Using cached file.")
    return send_file(file_path, as_attachment=True)

# ------------------------------------------------------------------------------
# Main Entry Point
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        prepare_saved_cache()
        read_remote_crc()
        read_saved_cache()

        network_discovery = NetworkDiscoverySdk(SERVICE_NAME, PORT)
        network_discovery.register()

        # Graceful shutdown on SIGTERM/SIGINT
        import signal

        def _on_shutdown(signum, frame):
            logger.info(f"Signal {signum} received, unregistering and tearing down…")
            try:
                network_discovery.unregister()
            except Exception:
                logger.exception("Error during Zeroconf unregister")
            teardown()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _on_shutdown)
        signal.signal(signal.SIGINT,  _on_shutdown)

        start_tcp_server(host="0.0.0.0", port=TCP_PORT)

        scheduler.init_app(app)
        scheduler.start()
        scheduler.add_job(id="clear_aged_cache", func=clear_aged_cache, trigger="interval", minutes=5)
        scheduler.add_job(id="saveJsonFiles",   func=saveJsonFiles,   trigger="interval", minutes=3)

        atexit.register(teardown)
        atexit.register(network_discovery.unregister)

        context = (
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "certs", "server.crt"),
            os.path.join(os.path.dirname(os.path.realpath(__file__)), "certs", "server.key")
        )
        logger.info("Starting Flask app with SSL context.")
        app.run(port=PORT, host="0.0.0.0", ssl_context=context, debug=False)

    except Exception as e:
        logger.exception("Main: Error occurred")
    finally:
        logger.info("Shutting down Esper EdgeBox.")
