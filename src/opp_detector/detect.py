import cv2
from insightface.app.person_analysis import PersonAnalysis
import signal
import time

import multiprocessing as mp
from multiprocessing.connection import Connection

class Detector:
	_anal:PersonAnalysis
	_detections:Connection
	_stop:Connection

	def __init__(self, anal:PersonAnalysis, stop_pipe:Connection, out_pipe:Connection):
		'''Initialize Detector with any PersonAnalysis instance'''
		self._anal = anal
		self._detections = out_pipe
		self._stop = stop_pipe

	def register(self, imgs:list[cv2.typing.MatLike], name:str):
		self._anal.register(name, imgs)

	def background_loop(self, cap:cv2.VideoCapture, target_frame_rate:float=5):
		'''The internal background loop task: consumes video frames and processes; send a notif to detection queue if opp detected'''

		if not cap.isOpened():
			raise RuntimeError("Capture is not opened")

		# throttle by wall clock: the camera's reported fps can't be trusted (mine says 15, delivers 30)
		min_interval:float = 1/target_frame_rate
		last_processed:float = 0.0

		# who -> conf, proportion of screen
		det_summary:dict[str,tuple[float,float]] = dict()

		while True:
			if self._stop.poll(): break

			ret, frame_raw = cap.read()
			if not ret:
				raise RuntimeError("No more video :(")

			# waitKey is what repaints the window + handles its events, so run it on EVERY frame
			# (skipped ones too) or the window freezes; press q to quit
			if cv2.waitKey(1) & 0xFF == ord("q"): break

			now = time.monotonic()
			if now - last_processed < min_interval: continue
			last_processed = now

			frame = _to_size(frame_raw)
			FRAME_AREA:int = frame.shape[0] * frame.shape[1]

			# now we do the face detection
			dets = self.detect(frame)
			unknown_count = 0
			for detection in dets:
				name = detection.person
				if name is None:
					unknown_count += 1
					name = f"unknown{unknown_count}"
				curr = det_summary.get(name,(0,0))
				summary = (
					max(curr[0], detection.similarity or 0),
					max(curr[1] + detection.area()//FRAME_AREA, 1 ) # extremely naive area calculation (should be fine for ourpurposes)
				)
				det_summary[name] = summary

			self.show_frame(frame, dets)

		self._detections.send(det_summary)

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
	return p, (stop_send, detection_recv)

def _detector_thread_start(people:dict[str,list[str]], stop_pipe:Connection, out_pipe:Connection):
	signal.signal(signal.SIGINT, signal.SIG_IGN) # so SIGINT doesn't hit subprocess directly

	anal = PersonAnalysis()
	detector = Detector(anal, stop_pipe, out_pipe)
	for name in people:
		img_paths = people[name]
		ims:list[cv2.typing.MatLike] = list(filter(lambda a: a is not None, map(cv2.imread, img_paths))) #type: ignore
		detector.register(ims,name)

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
		return {'bbox':self.bbox,'person':self.person}

def _to_size(img:cv2.typing.MatLike):
	'''Resizes img to processing size'''
	TARGET_HEIGHT = 480
	curr = img.shape
	# if curr[0] == 0 or curr[1] == 0: raise Exception("img has size 0 in atleast one dimension")
	new_size = ( curr[1] * TARGET_HEIGHT // curr[0], TARGET_HEIGHT )
	return cv2.resize(img,new_size) # maybe try resampling at later stage