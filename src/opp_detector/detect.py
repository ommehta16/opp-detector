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

	TARGET_FRAME_RATE = 2
	skip_frames = frame_rate/TARGET_FRAME_RATE

	frames:int = -1

	anal1 = PersonAnalysis()

	start = time.perf_counter()

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

		cv2.waitKey(1)

def detect_face(img: cv2.typing.MatLike,anal:PersonAnalysis):
	matches = anal.match(anal.get(img))
	anal.update(matches)

	for result in matches:
		person = result.observation
		t,l,b,r = person.body_bbox

		# TODO: draw t,l,b,r on img

	# TODO: show img to screen

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
	...

	# TODO: detect on the live camera feed of images
	#  -> show the live feed to the screen
