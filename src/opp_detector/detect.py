import cv2
from insightface.app.person_analysis import PersonAnalysis

import numpy as np
import time

def to_size(img:cv2.typing.MatLike):
	TARGET_HEIGHT = 480
	curr = img.shape
	# if curr[0] == 0 or curr[1] == 0: raise Exception("img has size 0 in atleast one dimension")
	new_size = ( curr[1] * TARGET_HEIGHT // curr[0], TARGET_HEIGHT )
	return cv2.resize(img,new_size) # maybe try resampling at later stage

def detect_face_loop(cap:cv2.VideoCapture,anal:PersonAnalysis,frame_rate:int=60):
	'''Precondition: cap is an open cv2 video capture'''
	if not cap.isOpened():
		raise RuntimeError("Capture is not opened")


	TARGET_FRAME_RATE = 5
	# whole number, otherwise `frames % skip_frames` never hits 0 for odd frame rates
	skip_frames = max(1, round(frame_rate/TARGET_FRAME_RATE))

	frames:int = -1

	while True:
		ret, frame_raw = cap.read()
		frames+=1

		if frames % skip_frames != 0:
			continue

		if not ret:
			raise RuntimeError("No more video :(")

		frame = to_size(frame_raw)

		# now we do the face detection

		detect_face(frame,anal)

		# waitKey also lets the window repaint; press q to quit
		if cv2.waitKey(1) & 0xFF == ord("q"):
			break

def detect_face(img: cv2.typing.MatLike,anal:PersonAnalysis):
	matches = anal.match(anal.get(img))
	anal.update(matches)

	for result in matches:
		person = result.observation
		# body_bbox is None when only a face was found -> fall back to the face box
		bbox = person.body_bbox if person.body_bbox is not None else person.face.bbox
		l,t,r,b = (int(v) for v in bbox) # insightface order is x1,y1,x2,y2 (floats)

		# green = the registered person, red = unknown
		known = result.person_id is not None
		color = (0,255,0) if known else (0,0,255)
		cv2.rectangle(img,(l,t),(r,b),color,2)
		cv2.putText(img,str(result.person_id) if known else "unknown",(l,max(t-8,15)),
			cv2.FONT_HERSHEY_SIMPLEX,0.6,color,2)

	cv2.imshow("opp-detector",img)

def single_img():
	
	img = cv2.imread("test_pics/reference.JPG")
	if img is None:
		raise FileNotFoundError("Image does not exist / could not be opened")
	reference = to_size(img)

	anal = PersonAnalysis()
	anal.prepare()
	registration = anal.register("me",reference)
	if registration.accepted == 0:
		raise ValueError(f"No registration accepted: {registration.rejected}")

	b = to_size(cv2.imread("test_pics/me2.png")) #type:ignore

	detect_face(b,anal)

def main():
	anal = PersonAnalysis()
	anal.prepare()
	# register() reads the path itself (as BGR)
	registration = anal.register("me","test_pics/reference.JPG")
	if registration.accepted == 0:
		raise ValueError(f"No registration accepted: {registration.rejected}")

	cap = cv2.VideoCapture(0) # 0 = default (built-in) camera
	try:
		# some cameras report 0 fps -> assume 30
		detect_face_loop(cap,anal,round(cap.get(cv2.CAP_PROP_FPS)) or 30)
	finally:
		cap.release()
		cv2.destroyAllWindows()

if __name__ == "__main__":
	main()