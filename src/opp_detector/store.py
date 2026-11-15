'''
Who we know about: one reference image per person, plus whether they're an opp.

Lives in `data/` under the current working directory (override with the OPP_DETECTOR_DATA env var):
	data/people.json        the list of people, in creation order
	data/images/<id>.<ext>  each person's image
'''
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

import cv2
import numpy as np

MAX_NAME_LEN = 60
MAX_IMAGE_BYTES = 15 * 1024 * 1024

DATA_ENV = "OPP_DETECTOR_DATA"

_IMAGE_FILE_RE = re.compile(r"[0-9a-f]{16}\.(jpg|png|webp)")


class StoreError(Exception):
	'''Something the caller got wrong; `status` is the HTTP status that fits'''
	def __init__(self, status:int, message:str):
		super().__init__(message)
		self.status = status
		self.message = message


def default_data_dir() -> Path:
	return Path(os.environ.get(DATA_ENV) or "data").resolve()

def sniff_image_ext(data:bytes) -> str|None:
	'''File extension for the image formats we accept, going by magic bytes; None for anything else'''
	if data[:3] == b"\xff\xd8\xff": return "jpg"
	if data[:8] == b"\x89PNG\r\n\x1a\n": return "png"
	if data[:4] == b"RIFF" and data[8:12] == b"WEBP": return "webp"
	return None


class Store:
	def __init__(self, data_dir:Path|str|None=None):
		self.dir = Path(data_dir).resolve() if data_dir is not None else default_data_dir()
		self.images_dir = self.dir / "images"
		self._json_path = self.dir / "people.json"
		self._lock = threading.Lock()
		self._people:list[dict] = [] # {id, name, opp, file, created}
		self._roster_version = 0 # bumped when someone is added/deleted (not on opp toggles)
		self._load()

	# ------------------------------------------------------------ reading

	def list(self) -> list[dict]:
		'''Everyone, in creation order, as API person objects'''
		with self._lock:
			return [_public(p) for p in self._people]

	def opp_ids(self) -> set[str]:
		with self._lock:
			return {p["id"] for p in self._people if p["opp"]}

	def roster_version(self) -> int:
		with self._lock:
			return self._roster_version

	def snapshot(self) -> tuple[list[dict], int]:
		'''(raw records incl. absolute `path` of each image, roster version), taken atomically'''
		with self._lock:
			people = [{**p, "path": str(self.images_dir / p["file"])} for p in self._people]
			return people, self._roster_version

	def image_path(self, filename:str) -> Path|None:
		'''Path of a stored image, or None if there's no such image (also for any funny filename)'''
		if not _IMAGE_FILE_RE.fullmatch(filename): return None
		path = self.images_dir / filename
		return path if path.is_file() else None

	# ------------------------------------------------------------ writing

	def add(self, name:str, opp:bool, image:bytes) -> dict:
		name = name.strip()
		if not name:
			raise StoreError(400, "Name can't be empty")
		if len(name) > MAX_NAME_LEN:
			raise StoreError(400, f"Name is too long (max {MAX_NAME_LEN} characters)")
		if not image:
			raise StoreError(400, "No image was uploaded")
		if len(image) > MAX_IMAGE_BYTES:
			raise StoreError(413, f"Image is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)")

		ext = sniff_image_ext(image)
		if ext is None:
			raise StoreError(400, "Unsupported image type: use a JPEG, PNG or WebP")
		if not _decodes(image):
			raise StoreError(400, "That file couldn't be read as an image")

		with self._lock:
			if any(p["name"].casefold() == name.casefold() for p in self._people):
				raise StoreError(409, f"There's already someone called {name!r}")

			person_id = secrets.token_hex(8)
			record = {"id": person_id, "name": name, "opp": bool(opp), "file": f"{person_id}.{ext}", "created": time.time()}

			self.images_dir.mkdir(parents=True, exist_ok=True)
			image_path = self.images_dir / record["file"]
			_write_atomic(image_path, image)
			try:
				self._save(self._people + [record])
			except BaseException:
				image_path.unlink(missing_ok=True)
				raise
			self._people.append(record)
			self._roster_version += 1
			return _public(record)

	def set_opp(self, person_id:str, opp:bool) -> dict:
		with self._lock:
			for i, p in enumerate(self._people):
				if p["id"] != person_id: continue
				updated = {**p, "opp": bool(opp)}
				people = self._people.copy()
				people[i] = updated
				self._save(people)
				self._people = people
				return _public(updated)
		raise StoreError(404, "No such person")

	def delete(self, person_id:str) -> None:
		with self._lock:
			for p in self._people:
				if p["id"] != person_id: continue
				self._save([q for q in self._people if q is not p])
				self._people.remove(p)
				self._roster_version += 1
				(self.images_dir / p["file"]).unlink(missing_ok=True)
				return
		raise StoreError(404, "No such person")

	# ------------------------------------------------------------ disk

	def _load(self) -> None:
		try:
			raw = self._json_path.read_text(encoding="utf-8")
		except FileNotFoundError:
			return
		try:
			people = json.loads(raw)["people"]
			self._people = [
				{"id": str(p["id"]), "name": str(p["name"]), "opp": bool(p["opp"]), "file": str(p["file"]), "created": p.get("created")}
				for p in people
			]
		except Exception as e:
			raise RuntimeError(f"{self._json_path} is corrupt ({e!r}): fix it or delete it to start over") from e

	def _save(self, people:list[dict]) -> None:
		self.dir.mkdir(parents=True, exist_ok=True)
		payload = json.dumps({"version": 1, "people": people}, indent=2, ensure_ascii=False)
		_write_atomic(self._json_path, payload.encode("utf-8"))


def _public(record:dict) -> dict:
	return {"id": record["id"], "name": record["name"], "opp": record["opp"], "image": f"/images/{record['file']}"}

def _decodes(image:bytes) -> bool:
	try:
		return cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR) is not None
	except Exception: # cv2.error on some malformed inputs
		return False

def _write_atomic(path:Path, data:bytes) -> None:
	'''Write to a temp file next to `path` then rename over it, so readers never see half a file'''
	tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
	try:
		with open(tmp, "wb") as f:
			f.write(data)
			f.flush()
			os.fsync(f.fileno())
		os.replace(tmp, path)
	except BaseException:
		tmp.unlink(missing_ok=True)
		raise
