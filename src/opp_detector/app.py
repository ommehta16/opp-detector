'''
The desktop app: add people (one photo each), tick who's an opp, start/stop the detector.

People live in `data/` under the current directory (override with the OPP_DETECTOR_DATA env var):
	data/people.json        [{id, name, opp, file}]
	data/images/<id>.jpg    each person's photo
'''
import json
import os
import secrets
import signal
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

from opp_detector.cli import is_edge
from opp_detector.detect import start_detector
from opp_detector.evader import choose_evasion_method

DATA = Path(os.environ.get("OPP_DETECTOR_DATA") or "data").resolve()
IMAGES = DATA / "images"
PEOPLE_JSON = DATA / "people.json"

THUMB_PX = 48
POLL_MS = 100


def load_people() -> list[dict]:
	try:
		return json.loads(PEOPLE_JSON.read_text(encoding="utf-8"))["people"]
	except FileNotFoundError:
		return []

def save_people(people:list[dict]) -> None:
	DATA.mkdir(parents=True, exist_ok=True)
	PEOPLE_JSON.write_text(json.dumps({"version": 1, "people": people}, indent=2), encoding="utf-8")


class App:
	def __init__(self, root:tk.Tk):
		self.root = root
		self.people = load_people()
		self.detector = None # (process, (stop_send, detection_recv)) while running
		self.error = "" # why the detector died, if it did
		self.frames = 0 # frames seen since start: 0 = still loading the model / opening the camera
		self.evasions = 0
		self.severity:list[float] = [0] * 5 # opps in frame, last 5 frames
		self.thumbs:dict[str, ImageTk.PhotoImage] = {} # tk drops images nobody holds on to

		root.title("Opp Detector")
		root.minsize(360, 0)
		root.resizable(False, False)
		root.protocol("WM_DELETE_WINDOW", self.quit)

		main = ttk.Frame(root, padding=16)
		main.pack(fill="both", expand=True)

		top = ttk.Frame(main)
		top.pack(fill="x")
		self.status = ttk.Label(top)
		self.status.pack(side="left")
		self.toggle = ttk.Button(top, command=self.toggle_detector)
		self.toggle.pack(side="right")

		ttk.Separator(main).pack(fill="x", pady=12)
		self.rows = ttk.Frame(main)
		self.rows.pack(fill="x")
		self.rows.columnconfigure(1, weight=1)
		ttk.Button(main, text="Add person…", command=self.add_person).pack(anchor="w", pady=(12, 0))

		self.render_people()
		self.refresh()
		self.poll()

	# ------------------------------------------------------------ people

	def render_people(self) -> None:
		for widget in self.rows.winfo_children():
			widget.destroy()
		if not self.people:
			ttk.Label(self.rows, text="Nobody yet. Add your boss.", foreground="gray").grid()

		for row, person in enumerate(self.people):
			ttk.Label(self.rows, image=self.thumb(person)).grid(row=row, column=0, pady=4) # type: ignore
			ttk.Label(self.rows, text=person["name"]).grid(row=row, column=1, sticky="w", padx=10)

			is_opp = tk.BooleanVar(value=person["opp"])
			ttk.Checkbutton(
				self.rows, text="Opp", variable=is_opp,
				command=lambda person=person, is_opp=is_opp: self.set_opp(person, is_opp.get()),
			).grid(row=row, column=2, padx=10)
			ttk.Button(
				self.rows, text="✕", width=2,
				command=lambda person=person: self.delete_person(person),
			).grid(row=row, column=3)

	def thumb(self, person:dict) -> ImageTk.PhotoImage|None:
		if person["id"] not in self.thumbs:
			try:
				img = Image.open(IMAGES / person["file"])
				img.draft("RGB", (THUMB_PX * 2, THUMB_PX * 2)) # decode big JPEGs small: much faster
				img = ImageOps.fit(ImageOps.exif_transpose(img).convert("RGB"), (THUMB_PX, THUMB_PX))
				self.thumbs[person["id"]] = ImageTk.PhotoImage(img)
			except Exception:
				return None # photo went missing: show the row without one
		return self.thumbs[person["id"]]

	def add_person(self) -> None:
		path = filedialog.askopenfilename(parent=self.root, title="Pick a photo of them")
		if not path: return
		try:
			img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
		except Exception:
			messagebox.showerror("Opp Detector", "That file couldn't be read as an image.", parent=self.root)
			return
		name = (simpledialog.askstring("Opp Detector", "Who is this?", parent=self.root) or "").strip()
		if not name: return

		# stored as JPEG whatever it came in as, so the detector (cv2) can always read it
		person_id = secrets.token_hex(8)
		IMAGES.mkdir(parents=True, exist_ok=True)
		img.save(IMAGES / f"{person_id}.jpg", quality=95)
		self.people.append({"id": person_id, "name": name, "opp": False, "file": f"{person_id}.jpg"})
		self.roster_changed()

	def delete_person(self, person:dict) -> None:
		if not messagebox.askyesno("Opp Detector", f"Remove {person['name']}?", parent=self.root): return
		self.people.remove(person)
		self.thumbs.pop(person["id"], None)
		(IMAGES / person["file"]).unlink(missing_ok=True)
		self.roster_changed()

	def set_opp(self, person:dict, opp:bool) -> None:
		# no restart needed: who counts as an opp is looked up on every frame
		person["opp"] = opp
		save_people(self.people)

	def roster_changed(self) -> None:
		save_people(self.people)
		self.render_people()
		if self.detector: # it only loads faces when it starts
			self.stop()
			if self.people: self.start()
		self.refresh()

	# ------------------------------------------------------------ detector

	def toggle_detector(self) -> None:
		if self.detector: self.stop()
		else: self.start()
		self.refresh()

	def start(self) -> None:
		# people go by id in there, so a name can't clash with the detector's "unknownN" labels
		self.detector = start_detector({p["id"]: [str(IMAGES / p["file"])] for p in self.people})
		self.error = ""
		self.frames = self.evasions = 0
		self.severity = [0] * 5

	def stop(self) -> None:
		process, (stop_send, detection_recv) = self.detector # type: ignore
		self.detector = None
		try:
			stop_send.send("stop")
		except OSError:
			pass # already gone
		process.join(2)
		if process.is_alive(): process.terminate() # e.g. still loading the model: not listening yet
		stop_send.close()
		detection_recv.close()

	def poll(self) -> None:
		'''Runs every POLL_MS on the UI thread: handle whatever frames the detector sent since'''
		try:
			if not self.detector: return
			process, (_, detection_recv) = self.detector
			try:
				while detection_recv.poll():
					self.on_frame(detection_recv.recv())
			except (EOFError, OSError):
				pass # the detector went away: picked up just below
			if not process.is_alive():
				self.stop()
				self.error = f"Detector died (exit code {process.exitcode}). Is the camera available?"
			self.refresh()
		finally:
			self.root.after(POLL_MS, self.poll)

	def on_frame(self, det_summary:dict[str, tuple[float, float]]) -> None:
		'''`det_summary` is person id -> (confidence, proportion of the frame)'''
		self.frames += 1
		opps = {p["id"] for p in self.people if p["opp"]}
		# same thresholds as cli.handle_detections
		in_frame = sum(
			1 for who, (conf, prop) in det_summary.items()
			if who in opps and conf >= 0.5 and prop >= 0.01
		)
		self.severity = self.severity[1:] + [in_frame]
		if is_edge(self.severity):
			self.evasions += 1
			choose_evasion_method()()

	# ------------------------------------------------------------ window

	def refresh(self) -> None:
		running = self.detector is not None
		if running:
			status = "Watching" if self.frames else "Starting…"
			if self.evasions: status += f" · {self.evasions} evasion{'s' if self.evasions > 1 else ''}"
		else:
			status = self.error or "Stopped"
		self.status.config(text=status)
		self.toggle.config(
			text="Stop detector" if running else "Start detector",
			state="normal" if running or self.people else "disabled",
		)

	def quit(self) -> None:
		if self.detector: self.stop()
		self.root.destroy()


def main() -> None:
	root = tk.Tk()
	app = App(root)
	signal.signal(signal.SIGINT, lambda *_: app.quit()) # ctrl-c in the terminal = closing the window
	root.mainloop()


if __name__ == "__main__":
	main()
