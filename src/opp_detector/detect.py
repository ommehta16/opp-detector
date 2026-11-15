import cv2
from insightface.app.person_analysis import PersonAnalysis
import signal
import time

import multiprocessing as mp
from multiprocessing.connection import Connection

SHOW_FEED = False

class Detector:
	_anal:PersonAnalysis
	_detections:Connection
	_stop:Connection

	def __init__(self, anal:PersonAnalysis, stop_pipe:Connection, out_pipe:Connection):
		'''Initialize Detector with any PersonAnalysis instance'''
		self._anal = anal
		self._detections = out_pipe
		self._stop = stop_pipe

	def register(self, imgs:list[cv2.typing.MatLike], name:str) -> bool:
		'''-> whether any of `imgs` was usable (a photo needs exactly one face in it)'''
		return bool(imgs) and self._anal.register(name, imgs).accepted > 0

	def handle_commands(self) -> bool:
		'''
		Does whatever the app sent down the stop pipe since last time -> False when it's time to stop

			("register", name, img_paths)   add someone without restarting (a restart reloads the model);
			                                answered with ("rejected", name) if no photo was usable
			("unregister", name)
			anything else                   stop
		'''
		try:
			while self._stop.poll():
				cmd = self._stop.recv()
				if not isinstance(cmd, tuple): return False
				if cmd[0] == "register":
					if not self.register(_read_imgs(cmd[2]), cmd[1]): self._detections.send(("rejected", cmd[1]))
				elif cmd[0] == "unregister":
					self._anal.remove_person(cmd[1])
		except EOFError: # the app is gone
			return False
		return True

	def background_loop(self, cap:cv2.VideoCapture, target_frame_rate:float=2, preview_frame_rate:float=10):
		'''
		The internal background loop task: consumes video frames and processes; sends the app
			("preview", jpeg)                  the frame at processing size, `preview_frame_rate` times a second
			("detections", summary, bboxes)    who is in frame, `target_frame_rate` times a second
		'''

		if not cap.isOpened():
			raise RuntimeError("Capture is not opened")

		# throttle by wall clock: the camera's reported fps can't be trusted (mine says 15, delivers 30)
		min_interval:float = 1/target_frame_rate
		last_processed:float = 0.0
		last_preview:float = 0.0

		while True:
			if not self.handle_commands(): break

			ret, frame_raw = cap.read()
			if not ret:
				raise RuntimeError("No more video :(")

			# waitKey is what repaints the window + handles its events, so run it on EVERY frame
			# (skipped ones too) or the window freezes; press q to quit
			if SHOW_FEED:
				if cv2.waitKey(1) & 0xFF == ord("q"): break

			now = time.monotonic()
			if now - last_preview >= 1/preview_frame_rate:
				last_preview = now
				# small (480p) on purpose: it's all the model looks at anyway, and it keeps the pipe cheap.
				# this send blocks until the app reads it, so a slow app slows us down instead of piling up frames
				self._detections.send(("preview", cv2.imencode(".jpg", _to_size(frame_raw))[1].tobytes()))

			if now - last_processed < min_interval: continue
			last_processed = now

			frame = _to_size(frame_raw)
			FRAME_AREA:int = frame.shape[0] * frame.shape[1]

			# now we do the face detection
			dets = self.detect(frame)
			# who -> conf, proportion of screen
			det_summary:dict[str,tuple[float,float]] = dict()
			unknown_count = 0
			for detection in dets:
				name = detection.person
				if name is None:
					unknown_count += 1
					name = f"unknown{unknown_count}"
				curr = det_summary.get(name,(0,0))
				summary = (
					max(curr[0], detection.similarity or 0),
					min(curr[1] + detection.area()/FRAME_AREA, 1 ) # extremely naive area calculation (should be fine for ourpurposes)
				)
				det_summary[name] = summary

			if SHOW_FEED:
				self.show_frame(frame, dets)

			self._detections.send(("detections", det_summary, [d.jason() for d in dets]))

	def detect(self, img:cv2.typing.MatLike) -> list[Detection]:
		matches = self._anal.match(self._anal.get(img))
		self._anal.update(matches)
		dets:list[Detection] = []
		for result in matches:
			person = result.observation
			# body_bbox is None when only a face was found -> fall back to the face box
			bbox = person.body_bbox if person.body_bbox is not None else person.face.bbox

			detection = Detection(bbox, result.person_id, result.similarity)
			dets.append(detection)

		return dets

	def show_frame(self, base_img:cv2.typing.MatLike, detections:list[Detection]):
		frame = base_img.copy()
		for detection in detections:
			detection.draw(frame)
		cv2.imshow("opp-detector",frame)

def start_detector(people:dict[str, list[str]]):
	''' -> process, stop_pipe, detection_pipe'''
	# two separate one-way pipes: mp.Pipe() returns the two ENDS of a single pipe, so using one
	# pipe for both made the detector's own detections show up as its stop signal
	stop_recv, stop_send = mp.Pipe(duplex=False)
	detection_recv, detection_send = mp.Pipe(duplex=False)
	p = mp.Process(target=_detector_thread_start,args=[people,stop_recv,detection_send],daemon=True)
	p.start() # .run() would run the target right here in this process and block until it returns
	# the child has its own copies of its two ends now; drop ours so they don't pile up across
	# restarts, and so reading detections hits EOF once the child is gone
	stop_recv.close()
	detection_send.close()
	return p, (stop_send, detection_recv)

def _detector_thread_start(people:dict[str,list[str]], stop_pipe:Connection, out_pipe:Connection):
	signal.signal(signal.SIGINT, signal.SIG_IGN) # so SIGINT doesn't hit subprocess directly

	anal = PersonAnalysis()
	detector = Detector(anal, stop_pipe, out_pipe)
	for name in people:
		detector.register(_read_imgs(people[name]),name)

	cap = cv2.VideoCapture(0) 
	try:
		detector.background_loop(cap)
	finally:
		cap.release()
		cv2.destroyAllWindows()

class Detection:
	bbox:tuple[int,int,int,int]
	person:str|None
	similarity:float|None

	def __init__(self, bbox:tuple[float,float,float,float], person:str|None=None, similarity:float|None=None):
		'''
		Create `Detection` with given bbox (l,t,r,b) and person
		
		Args:
			- bbox = axis aligned bounding box of deteciton
			- person = name of the identified person
			- similarity = cosine similarity
		'''
		self.bbox = tuple(int(v) for v in bbox) #type: ignore
		self.person = person
		self.similarity = similarity

	def draw(self,img:cv2.typing.MatLike):
		'''Draws this detection onto `img` in-place (mutates `img`!)'''
		l, t, r, b = self.bbox
		color = (255,0,0) if self.person is not None else (0,255,0)
		cv2.rectangle(img, (l,t), (r,b), color,2)
		cv2.putText(img,
					self.person if self.person is not None else "unknown",
					(l,max(t-8,15)),
					fontFace = cv2.FONT_HERSHEY_SIMPLEX,
					fontScale = 0.6,
					color = color,
					thickness = 2)
	def area(self):
		l, t, r, b = self.bbox
		return abs((r-l) * (b-t))
	
	def jason(self):
		return {'bbox':self.bbox,'person':self.person,'similarity':self.similarity}

def _read_imgs(img_paths:list[str]) -> list[cv2.typing.MatLike]:
	return list(filter(lambda a: a is not None, map(cv2.imread, img_paths))) #type: ignore

def _to_size(img:cv2.typing.MatLike):
	'''Resizes img to processing size'''
	TARGET_HEIGHT = 480
	curr = img.shape
	# if curr[0] == 0 or curr[1] == 0: raise Exception("img has size 0 in atleast one dimension")
	new_size = ( curr[1] * TARGET_HEIGHT // curr[0], TARGET_HEIGHT )
	return cv2.resize(img,new_size) # maybe try resampling at later stage