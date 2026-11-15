import cv2
from insightface.app.person_analysis import PersonAnalysis
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

	def background_loop(self, cap:cv2.VideoCapture, frame_rate:int=60, target_frame_rate:int=5):
		'''The internal background loop task: consumes video frames and processes; send a notif to detection queue if opp detected'''
		
		if not cap.isOpened():
			raise RuntimeError("Capture is not opened")
		
		# whole number, otherwise `frames % skip_frames` never hits 0 for odd frame rates
		skip_frames:int = max(1, round(frame_rate/target_frame_rate))
	
		frames:int = -1
	
		while True:
			if self._stop.poll(): break
			
			ret, frame_raw = cap.read()
			frames+=1
	
			if frames % skip_frames != 0:
				continue
	
			if not ret:
				raise RuntimeError("No more video :(")
	
			frame = _to_size(frame_raw)
	
			# now we do the face detection
			dets = self.detect(frame)
			print("hi!")
			for det in dets: self._detections.send("hello?")

			self.show_frame(frame, dets)
			# waitKey also lets the window repaint; press q to quit
			if cv2.waitKey(1) & 0xFF == ord("q"): break

	def detect(self, img:cv2.typing.MatLike) -> list[Detection]:
		matches = self._anal.match(self._anal.get(img))
		self._anal.update(matches)
		dets:list[Detection] = []
		for result in matches:
			person = result.observation
			# body_bbox is None when only a face was found -> fall back to the face box
			bbox = person.body_bbox if person.body_bbox is not None else person.face.bbox

			detection = Detection(bbox, result.person_id)
			dets.append(detection)

		return dets

	def show_frame(self, base_img:cv2.typing.MatLike, detections:list[Detection]):
		frame = base_img.copy()
		for detection in detections:
			detection.draw(frame)
		cv2.imshow("opp-detector",frame)

def start_detector(people:dict[str, list[str]]):
	''' -> process, stop_pipe, detection_pipe'''
	stop_pipe, detection_pipe = mp.Pipe()
	p = mp.Process(target=_detector_thread_start,args=[people,stop_pipe,detection_pipe])
	p.run()
	return p, (stop_pipe, detection_pipe)

def _detector_thread_start(people:dict[str,list[str]], stop_pipe:Connection, out_pipe:Connection):
	anal = PersonAnalysis()
	detector = Detector(anal, stop_pipe, out_pipe)
	for name in people:
		img_paths = people[name]
		ims:list[cv2.typing.MatLike] = list(filter(lambda a: a is not None, map(cv2.imread, img_paths))) #type: ignore
		detector.register(ims,name)

	detector.background_loop( cv2.VideoCapture(0) )

class Detection:
	bbox:tuple[int,int,int,int]
	person:str|None

	def __init__(self, bbox:tuple[float,float,float,float], person:str|None):
		'''Create `Detection` with given bbox (l,t,r,b) and person'''
		self.bbox = (int(v) for v in bbox) #type: ignore
		self.person = person

	def draw(self,img:cv2.typing.MatLike):
		'''Draws this detection onto `img` in-place (mutates `img`!)'''
		l, r, t, b = self.bbox
		color = (255,0,0) if self.person is not None else (0,255,0)
		cv2.rectangle(img, (l,t), (r,b), color,2)
		cv2.putText(img,
					self.person if self.person is not None else "unknown",
					(l,max(t-8,15)),
					fontFace = cv2.FONT_HERSHEY_SIMPLEX,
					fontScale = 0.6,
					color = color,
					thickness = 2)
	def jason(self):
		return {'bbox':self.bbox,'person':self.person}

def _to_size(img:cv2.typing.MatLike):
	'''Resizes img to processing size'''
	TARGET_HEIGHT = 480
	curr = img.shape
	# if curr[0] == 0 or curr[1] == 0: raise Exception("img has size 0 in atleast one dimension")
	new_size = ( curr[1] * TARGET_HEIGHT // curr[0], TARGET_HEIGHT )
	return cv2.resize(img,new_size) # maybe try resampling at later stage

def main():
	p, (stop_pipe, detection_pipe) = start_detector({"me":["test_pics/reference.JPG",]})

	try:
		while True:
			time.sleep(10)
			while detection_pipe.poll():
				print(detection_pipe.recv())
	except:
		stop_pipe.send("stopppp")
		p.join()

	anal = PersonAnalysis()
	anal.prepare()
	# register() reads the path itself (as BGR)
	registration = anal.register("me","test_pics/reference.JPG")
	if registration.accepted == 0:
		raise ValueError(f"No registration accepted: {registration.rejected}")

	cap = cv2.VideoCapture(0) # 0 = default (built-in) camera
	try:
		# some cameras report 0 fps -> assume 30
		# detect_face_loop(cap,anal,round(cap.get(cv2.CAP_PROP_FPS)) or 30)
		...
	finally:
		cap.release()
		cv2.destroyAllWindows()

if __name__ == "__main__":
	main()