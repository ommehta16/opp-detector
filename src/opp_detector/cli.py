from opp_detector.detect import start_detector
from multiprocessing import Process
from multiprocessing.connection import Connection

people = {
    "om":['test_pics/reference.JPG']
}
opps = {"om"}

def main() -> None:    
    p, (stop_send, detection_recv) = start_detector(people)

    try:
        handle_detections(p, detection_recv)


            
    except: pass
    finally:
        stop_send.send("stopppp")
        p.join()

def handle_detections(p:Process, detection_recv: Connection):
    # name -> (start time, end time)
    # remove when end time is 5 secs ago
    # opps_in_frame:dict[str, tuple[float,float]] = dict()

    # stops by itself when the detector exits (q pressed in the window, or it crashed)

    # severity for last 5 frames (list is honestly probably faster than deque here lol)
    severity:list[float] = [0,0,0,0,0]

    while p.is_alive():
        if not detection_recv.poll(1): continue

        # name -> conf, prop of screen
        det_summary:dict[str, tuple[float,float]] = detection_recv.recv()
        print(det_summary)

        curr_severity = 0

        for person in det_summary:
            if person not in opps: continue

            conf, prop = det_summary[person]
            if conf < 0.8 or prop < 0.01: continue

            curr_severity += 1 # 1 opp on screen so we increase severity

        severity = [severity[i] for i in range(1,5)] + [curr_severity]
        
        if is_edge(severity):
            # TODO: do evasion!
            print("I WOULD EVADE YOU HERE YOU STINKY MONKEY")
            ...

def is_edge(severity:list[float]):
    start_avg = sum(severity[:-2])/(len(severity) - 1)
    end = severity[-1]
    if end > start_avg + 0.1: return True

# UNUSED!
def calc_severity(opps_in_frame:dict[str, tuple[float,float]]):
    '''Evaluate the severity of the current situation (how extremely we need to act to evade opps)'''

    total = 0

    for opp in opps_in_frame:
        start, end = opps_in_frame[opp]
        duration += end-start
        total += (duration/10)**2

    total = max(total,10)
    return total


if __name__ == "__main__":
    main()