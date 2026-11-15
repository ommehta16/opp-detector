'''
The desktop app: add people (one photo each), tick who's an opp, start/stop the detector, and
watch what it sees (camera view with a box on everyone + a big CLEAR / OPP IN FRAME banner).

People live in `data/` under the current directory (override with the OPP_DETECTOR_DATA env var):
	data/people.json        [{id, name, opp, file}]
	data/images/<id>.jpg    each person's photo
'''
import io
import json
import os
import secrets
import signal
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

from opp_detector.cli import is_edge
from opp_detector.detect import start_detector
from opp_detector.evader import choose_evasion_method

DATA = Path(os.environ.get("OPP_DETECTOR_DATA") or "data").resolve()
IMAGES = DATA / "images"
PEOPLE_JSON = DATA / "people.json"

THUMB_PX = 48
POLL_MS = 30 # well ahead of the previews' 10 a second: the detector waits on us to read each one
VIEW_W = 640
RED, GREEN, GREY = "#d92b2b", "#1e9e4a", "#555555"
FONT = ImageFont.load_default(24)


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
		self.in_frame:list[str] = [] # ...and their names, latest frame
		self.bboxes:list[dict] = [] # everyone in the latest frame (Detection.jason())
		self.jpeg:bytes|None = None # the latest camera preview
		self.thumbs:dict[str, ImageTk.PhotoImage] = {} # tk drops images nobody holds on to
		self.blank = ImageTk.PhotoImage(Image.new("RGB", (VIEW_W, VIEW_W * 9 // 16), "#1c1c1c")) # camera off
		self.photo = self.blank # what the camera view is showing

		root.title("Opp Detector")
		root.minsize(360, 0)
		root.resizable(False, False)
		root.protocol("WM_DELETE_WINDOW", self.quit)

		outer = ttk.Frame(root, padding=16)
		outer.pack(fill="both", expand=True)

		# left: banner + camera view. tk.Label because ttk ignores background colours on macOS
		left = ttk.Frame(outer)
		left.pack(side="left", anchor="n", padx=(0, 16))
		self.banner = tk.Label(left, fg="white", font=("Helvetica", 24, "bold"), pady=8)
		self.banner.pack(fill="x")
		self.view = tk.Label(left, bd=0, image=self.blank)
		self.view.pack()

		# right: everything else
		main = ttk.Frame(outer)
		main.pack(side="left", anchor="n", fill="both", expand=True)

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
		buttons = ttk.Frame(main)
		buttons.pack(anchor="w", pady=(12, 0))
		ttk.Button(buttons, text="Add person…", command=self.add_person).pack(side="left")
		self.capture = ttk.Button(buttons, text="Capture from camera", command=self.capture_person)
		self.capture.pack(side="left", padx=(8, 0))

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
		self.enrol(img, opp=False)

	def capture_person(self) -> None:
		'''Whoever the camera sees right now becomes an opp ("you're my boss now")'''
		# the 480p preview frame is all it takes: it's the size the detector looks at anyway
		if self.jpeg: self.enrol(Image.open(io.BytesIO(self.jpeg)), opp=True)

	def enrol(self, img:Image.Image, opp:bool) -> None:
		name = (simpledialog.askstring("Opp Detector", "Who is this?", parent=self.root) or "").strip()
		if not name: return

		# stored as JPEG whatever it came in as, so the detector (cv2) can always read it
		person_id = secrets.token_hex(8)
		IMAGES.mkdir(parents=True, exist_ok=True)
		img.save(IMAGES / f"{person_id}.jpg", quality=95)
		self.people.append({"id": person_id, "name": name, "opp": opp, "file": f"{person_id}.jpg"})
		# no restart (that reloads the model); it answers "rejected" if it can't find the face
		self.send(("register", person_id, [str(IMAGES / f"{person_id}.jpg")]))
		self.roster_changed()

	def delete_person(self, person:dict) -> None:
		if not messagebox.askyesno("Opp Detector", f"Remove {person['name']}?", parent=self.root): return
		self.send(("unregister", person["id"]))
		self.remove(person)

	def remove(self, person:dict) -> None:
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
		self.in_frame, self.bboxes = [], []

	def stop(self) -> None:
		process, (stop_send, detection_recv) = self.detector # type: ignore
		self.send("stop")
		self.detector = None
		# keep reading while it winds down: it can't get to the stop while it's waiting on us to take a preview
		deadline = time.monotonic() + 2
		try:
			while process.is_alive() and time.monotonic() < deadline:
				if detection_recv.poll(0.05): detection_recv.recv()
		except (EOFError, OSError):
			process.join(1) # it closed its end: on its way out
		if process.is_alive(): process.terminate() # e.g. still loading the model: not listening yet
		stop_send.close()
		detection_recv.close()
		self.jpeg = None
		self.photo = self.blank
		self.view.config(image=self.blank)

	def send(self, cmd) -> None:
		'''Tell the detector something (see Detector.handle_commands), if it's running'''
		if not self.detector: return
		try:
			self.detector[1][0].send(cmd)
		except OSError:
			pass # already gone

	def poll(self) -> None:
		'''Runs every POLL_MS on the UI thread: handle whatever frames the detector sent since'''
		try:
			if not self.detector: return
			process, (_, detection_recv) = self.detector
			try:
				while detection_recv.poll():
					kind, *msg = detection_recv.recv()
					if kind == "preview": self.show(*msg)
					elif kind == "detections": self.on_frame(*msg)
					elif kind == "rejected": self.on_rejected(*msg)
			except (EOFError, OSError):
				pass # the detector went away: picked up just below
			if not process.is_alive():
				self.stop()
				self.error = f"Detector died (exit code {process.exitcode}). Is the camera available?"
			self.refresh()
		finally:
			self.root.after(POLL_MS, self.poll)

	def show(self, jpeg:bytes) -> None:
		'''Put a camera frame in the view: mirrored like a selfie camera, a box + name on everyone the detector saw'''
		self.jpeg = jpeg
		img = ImageOps.mirror(Image.open(io.BytesIO(jpeg)))
		draw = ImageDraw.Draw(img)
		people = {p["id"]: p for p in self.people}
		for det in self.bboxes:
			person = people.get(det["person"]) # None = a stranger (or deleted since)
			colour = GREY if person is None else RED if person["opp"] else GREEN
			l, t, r, b = det["bbox"]
			l, r = img.width - r, img.width - l # the picture is flipped, so the box flips with it
			draw.rectangle((l, t, r, b), outline=colour, width=4)
			# drawn after the flip, so it reads the right way round
			label = f"{person['name']} {det['similarity'] or 0:.0%}" if person else "unknown"
			draw.text((l + 8, max(t, 0) + 6), label, font=FONT, fill="white", stroke_width=3, stroke_fill=colour)
		self.photo = ImageTk.PhotoImage(img.resize((VIEW_W, VIEW_W * img.height // img.width)))
		self.view.config(image=self.photo)

	def on_rejected(self, person_id:str) -> None:
		'''The detector couldn't use the photo of someone we just added: take them back out'''
		for person in [p for p in self.people if p["id"] == person_id]:
			self.remove(person)
			messagebox.showerror("Opp Detector", f"Couldn't add {person['name']}: their photo needs exactly one face in it.", parent=self.root)

	def on_frame(self, det_summary:dict[str, tuple[float, float]], bboxes:list[dict]) -> None:
		'''`det_summary` is person id -> (confidence, proportion of the frame)'''
		self.frames += 1
		self.bboxes = bboxes
		opps = {p["id"]: p["name"] for p in self.people if p["opp"]}
		# same thresholds as cli.handle_detections
		self.in_frame = [
			opps[who] for who, (conf, prop) in det_summary.items()
			if who in opps and conf >= 0.5 and prop >= 0.01
		]
		self.severity = self.severity[1:] + [len(self.in_frame)]
		if is_edge(self.severity):
			self.evasions += 1
			# evading blocks for up to a second or two: get the reason on screen first
			self.refresh()
			self.root.update_idletasks()
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
		# startable with nobody added yet: the first person can come straight from the camera
		self.toggle.config(text="Stop detector" if running else "Start detector")
		self.capture.config(state="normal" if self.jpeg else "disabled")

		if not running: self.banner.config(text="OFF", bg=GREY)
		elif not self.frames: self.banner.config(text="STARTING…", bg=GREY)
		elif self.in_frame: self.banner.config(text="OPP IN FRAME: " + ", ".join(self.in_frame), bg=RED)
		else: self.banner.config(text="CLEAR", bg=GREEN)

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
