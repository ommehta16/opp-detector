'''
The local web UI's backend: a tiny JSON API over the people store, plus the detector's on/off switch.

Binds to 127.0.0.1 only. See `Handler` for the routes.
'''
import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from opp_detector.detect import start_detector
from opp_detector.evader import choose_evasion_method
from opp_detector.store import MAX_IMAGE_BYTES, Store, StoreError

log = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC_DIR = Path(__file__).parent / "static"

_IMAGE_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

# bodies over the limit are still read (and thrown away) up to this size so the client gets to
# see the 413 instead of a connection reset mid-upload
_DRAIN_LIMIT = 64 * 1024 * 1024
_MAX_JSON_BYTES = 64 * 1024

# how long the detector gets to stop by itself before we terminate it
_STOP_TIMEOUT_S = 5


# ---------------------------------------------------------------- edge detection

class EdgeDetector:
	'''Decides when an opp has just shown up: a rising edge in severity, at most once per DEBOUNCE_S'''
	WINDOW = 5
	DEBOUNCE_S = 5

	def __init__(self) -> None:
		# severity for the last WINDOW frames
		self._severity:list[float] = [0] * self.WINDOW
		self._last_edge:float = 0

	def push(self, curr_severity:float) -> bool:
		'''Feed in this frame's severity; returns whether to evade now'''
		self._severity = self._severity[1:] + [curr_severity]

		start = self._severity[:-2]
		start_avg = sum(start) / len(start)
		end = self._severity[-1]
		if end > start_avg + 0.1 and time.monotonic() - self._last_edge > self.DEBOUNCE_S:
			self._last_edge = time.monotonic()
			return True
		return False


def frame_severity(det_summary:dict[str, tuple[float, float]], opps:set[str]) -> int:
	'''How many opps are convincingly in this frame. `det_summary` is who -> (conf, prop of screen)'''
	severity = 0
	for person, (conf, prop) in det_summary.items():
		if person not in opps: continue
		if conf < 0.5 or prop < 0.01: continue
		severity += 1 # 1 opp on screen so we increase severity
	return severity


def _evade() -> None:
	choose_evasion_method()()


# ---------------------------------------------------------------- detector controller

class _Session:
	'''One run of the detector process'''
	def __init__(self, process, stop_send, detection_recv, roster_version:int):
		self.process = process
		self.stop_send = stop_send
		self.detection_recv = detection_recv
		self.roster_version = roster_version
		self.stopping = False
		self.thread:threading.Thread|None = None


class DetectorError(Exception):
	'''start() refused; the message is fit to show the user'''


class DetectorController:
	'''
	Starts/stops the detector process and watches its detections on a background thread.

	The detector knows people by their store ID, and the set of opps is read from the store on
	every frame, so flagging/unflagging someone takes effect immediately. Adding or deleting
	someone needs a restart (the process only loads faces when it starts) -> `stale`.
	'''
	def __init__(self, store:Store, spawn:Callable=start_detector, evade:Callable[[], None]=_evade):
		self._store = store
		self._spawn = spawn
		self._evade = evade
		self._op_lock = threading.Lock() # serialises start/stop (stop can take seconds)
		self._lock = threading.Lock() # guards the fields below; never held for long
		self._session:_Session|None = None
		self._evasions = 0
		self._last_evasion:float|None = None
		self._error:str|None = None

	def status(self) -> dict:
		with self._lock:
			session = self._session
			if session is not None and not session.process.is_alive():
				self._mark_died(session)
				session = None
			return {
				"running": session is not None,
				"stale": session is not None and session.roster_version != self._store.roster_version(),
				"evasions": self._evasions,
				"last_evasion": self._last_evasion,
				"error": self._error,
			}

	def start(self) -> dict:
		with self._op_lock:
			with self._lock:
				session = self._session
				if session is not None and not session.process.is_alive():
					self._mark_died(session)
					session = None
			if session is not None:
				return self.status()

			people, roster_version = self._store.snapshot()
			if not people:
				raise DetectorError("Add at least one person before starting the detector")
			if not any(p["opp"] for p in people):
				raise DetectorError("Flag at least one person as an opp before starting the detector")

			process, (stop_send, detection_recv) = self._spawn({p["id"]: [p["path"]] for p in people})
			session = _Session(process, stop_send, detection_recv, roster_version)
			session.thread = threading.Thread(target=self._watch, args=[session], name="opp-watch", daemon=True)
			with self._lock:
				self._session = session
				self._evasions = 0
				self._last_evasion = None
				self._error = None
			session.thread.start()
		return self.status()

	def stop(self) -> dict:
		with self._op_lock:
			with self._lock:
				session = self._session
				self._session = None
				if session is not None:
					session.stopping = True
			if session is not None:
				self._shut_down(session)
		return self.status()

	def _shut_down(self, session:_Session) -> None:
		process = session.process
		try:
			session.stop_send.send("stop")
		except OSError:
			pass # already gone
		process.join(_STOP_TIMEOUT_S)
		if process.is_alive():
			log.warning("detector didn't stop within %ss: terminating it", _STOP_TIMEOUT_S)
			process.terminate()
			process.join(2)
			if process.is_alive():
				process.kill()
				process.join(2)
		if session.thread is not None:
			session.thread.join(3)
		for conn in (session.stop_send, session.detection_recv):
			try: conn.close()
			except OSError: pass

	def _mark_died(self, session:_Session) -> None:
		'''The detector went away without being asked to. Call with `_lock` held'''
		if self._session is not session or session.stopping: return
		self._session = None
		code = session.process.exitcode
		self._error = f"Detector exited unexpectedly (exit code {code}). Is the camera available and allowed for your terminal app?"
		log.error("%s", self._error)

	def _watch(self, session:_Session) -> None:
		'''Background thread: turns the detector's per-frame summaries into evasions'''
		edges = EdgeDetector()
		process, detection_recv = session.process, session.detection_recv

		while not session.stopping:
			try:
				if not detection_recv.poll(1):
					if not process.is_alive(): break
					continue
				# person id -> conf, prop of screen
				det_summary:dict[str, tuple[float, float]] = detection_recv.recv()
			except (EOFError, OSError):
				break # detector exited (or stop() closed the pipe under us)

			if not edges.push(frame_severity(det_summary, self._store.opp_ids())): continue
			if session.stopping: break

			with self._lock:
				if self._session is session:
					self._evasions += 1
					self._last_evasion = time.time()
			log.info("opp spotted: evading")
			try:
				self._evade()
			except Exception:
				log.exception("evasion failed")

		if not session.stopping:
			process.join(2) # so there's an exit code to report
			with self._lock:
				self._mark_died(session)


# ---------------------------------------------------------------- http

class _HttpError(Exception):
	def __init__(self, status:int, message:str):
		super().__init__(message)
		self.status = status
		self.message = message


class Handler(BaseHTTPRequestHandler):
	'''
	GET    /                      static/index.html
	GET    /static/<path>         anything else under static/
	GET    /images/<file>         a person's image
	GET    /api/people            everyone
	POST   /api/people?name=&opp= add someone; the body is the raw image
	PATCH  /api/people/<id>       {"opp": bool}
	DELETE /api/people/<id>
	GET    /api/detector          status
	POST   /api/detector/start
	POST   /api/detector/stop
	'''
	server:"Server"
	server_version = "opp-detector"

	def do_GET(self) -> None: self._dispatch("GET")
	def do_POST(self) -> None: self._dispatch("POST")
	def do_PATCH(self) -> None: self._dispatch("PATCH")
	def do_DELETE(self) -> None: self._dispatch("DELETE")
	def do_PUT(self) -> None: self._dispatch("PUT") # -> a JSON 405/404 rather than http.server's HTML 501

	def log_message(self, format:str, *args) -> None:
		# the page polls every couple of seconds: per-request logging would be all noise
		log.debug("%s %s", self.address_string(), format % args)

	# ------------------------------------------------------------ routing

	def _dispatch(self, method:str) -> None:
		try:
			self._check_local()
			url = urlsplit(self.path)
			self._route(method, url.path, parse_qs(url.query, keep_blank_values=True))
		except (_HttpError, StoreError) as e:
			self._send_error(e.status, e.message)
		except DetectorError as e:
			self._send_error(400, str(e))
		except (BrokenPipeError, ConnectionResetError):
			self.close_connection = True # client went away mid-response
		except Exception as e:
			log.exception("unhandled error for %s %s", method, self.path)
			try: self._send_error(500, f"Internal error: {e}")
			except OSError: pass

	def _route(self, method:str, path:str, query:dict[str, list[str]]) -> None:
		store, detector = self.server.store, self.server.detector

		if path == "/api/people":
			if method == "GET": return self._send_json(200, store.list())
			if method == "POST": return self._add_person(query)
			raise _HttpError(405, "Method not allowed")

		if path.startswith("/api/people/"):
			person_id = unquote(path.removeprefix("/api/people/"))
			if method == "PATCH":
				body = self._read_json()
				if not isinstance(body, dict) or not isinstance(body.get("opp"), bool):
					raise _HttpError(400, 'Expected a JSON body like {"opp": true}')
				return self._send_json(200, store.set_opp(person_id, body["opp"]))
			if method == "DELETE":
				store.delete(person_id)
				return self._send_empty(204)
			raise _HttpError(405, "Method not allowed")

		if path == "/api/detector":
			if method == "GET": return self._send_json(200, detector.status())
			raise _HttpError(405, "Method not allowed")

		if path in ("/api/detector/start", "/api/detector/stop"):
			if method != "POST": raise _HttpError(405, "Method not allowed")
			self._drain_body()
			return self._send_json(200, detector.start() if path.endswith("/start") else detector.stop())

		if method == "GET":
			if path == "/":
				return self._send_file(STATIC_DIR / "index.html", cache=False)
			if path.startswith("/static/"):
				return self._send_file(_static_path(unquote(path.removeprefix("/static/"))), cache=False)
			if path.startswith("/images/"):
				image = store.image_path(unquote(path.removeprefix("/images/")))
				if image is None: raise _HttpError(404, "No such image")
				# image filenames are never reused, so they can be cached for good
				return self._send_file(image, content_type=_IMAGE_TYPES[image.suffix], cache=True)

		raise _HttpError(404, "Not found")

	def _add_person(self, query:dict[str, list[str]]) -> None:
		length = self._content_length()
		if length > MAX_IMAGE_BYTES:
			self._drain_body()
			raise _HttpError(413, f"Image is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)")
		image = self._read_body(length)

		name = query.get("name", [""])[-1]
		opp_raw = query.get("opp", ["0"])[-1].lower()
		if opp_raw in ("1", "true"): opp = True
		elif opp_raw in ("0", "false", ""): opp = False
		else: raise _HttpError(400, "opp must be 0 or 1")

		self._send_json(201, self.server.store.add(name, opp, image))

	def _check_local(self) -> None:
		'''
		Only answer requests addressed to this machine, made by our own page (or a non-browser).
		Without this any website open in the browser could POST here (or reach us through DNS
		rebinding) and switch the detector on.
		'''
		host = self.headers.get("Host", "")
		hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
		if hostname.lower() not in ("127.0.0.1", "localhost", "[::1]"):
			raise _HttpError(403, "Forbidden: this server only answers to localhost")
		origin = self.headers.get("Origin")
		if origin is not None and origin.lower() != f"http://{host}".lower():
			raise _HttpError(403, "Forbidden: cross-origin request")

	# ------------------------------------------------------------ request bodies

	def _content_length(self) -> int:
		raw = self.headers.get("Content-Length")
		if raw is None:
			if self.headers.get("Transfer-Encoding"):
				self.close_connection = True
				raise _HttpError(411, "Content-Length is required")
			return 0
		try:
			length = int(raw)
		except ValueError:
			length = -1
		if length < 0:
			self.close_connection = True
			raise _HttpError(400, "Bad Content-Length")
		return length

	def _read_body(self, length:int) -> bytes:
		body = self.rfile.read(length) if length else b""
		if len(body) != length:
			self.close_connection = True
			raise _HttpError(400, "Request body was cut short")
		return body

	def _drain_body(self) -> None:
		'''Read and discard the request body'''
		remaining = self._content_length()
		if remaining > _DRAIN_LIMIT:
			self.close_connection = True
			return
		while remaining > 0:
			chunk = self.rfile.read(min(remaining, 1 << 16))
			if not chunk: break
			remaining -= len(chunk)

	def _read_json(self):
		length = self._content_length()
		if length > _MAX_JSON_BYTES:
			self._drain_body()
			raise _HttpError(413, "Request body is too large")
		try:
			return json.loads(self._read_body(length))
		except ValueError: # incl. bad utf-8
			raise _HttpError(400, "Request body isn't valid JSON") from None

	# ------------------------------------------------------------ responses

	def _send_json(self, status:int, payload) -> None:
		body = json.dumps(payload).encode("utf-8")
		self.send_response(status)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(body)

	def _send_error(self, status:int, message:str) -> None:
		self._send_json(status, {"error": message})

	def _send_empty(self, status:int) -> None:
		self.send_response(status)
		self.send_header("Content-Length", "0")
		self.end_headers()

	def _send_file(self, path:Path|None, content_type:str|None=None, cache:bool=False) -> None:
		try:
			if path is None or not path.is_file(): raise FileNotFoundError
			body = path.read_bytes()
		except OSError:
			raise _HttpError(404, "Not found") from None
		if content_type is None:
			content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
			if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
				content_type += "; charset=utf-8"
		self.send_response(200)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(body)))
		self.send_header("Cache-Control", "private, max-age=31536000, immutable" if cache else "no-cache")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.end_headers()
		self.wfile.write(body)


def _static_path(relative:str) -> Path|None:
	'''The file under static/ that `relative` names, or None if it would escape static/'''
	if not relative or "\x00" in relative: return None
	root = STATIC_DIR.resolve()
	path = (root / relative).resolve()
	return path if path.is_relative_to(root) and path != root else None


class Server(ThreadingHTTPServer):
	daemon_threads = True # don't let a lingering connection hold up shutdown

	def __init__(self, store:Store, detector:DetectorController, port:int=DEFAULT_PORT):
		self.store = store
		self.detector = detector
		super().__init__((HOST, port), Handler)

	@property
	def url(self) -> str:
		return f"http://{HOST}:{self.server_address[1]}/"
