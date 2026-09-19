import cv2
import ultralytics as ut

model = ut.YOLO("yolov8n.pt")

CONF = 0.4
PERSON_CLASS = 0


def detect(source, show=True, save=False):
    results = model(source, conf=CONF, classes=[PERSON_CLASS], verbose=False)

    for r in results:
        boxes = r.boxes
        for box in boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0])
            cls = int(box.cls[0])
            print(f"person {conf:.2f} [{x1},{y1},{x2},{y2}]")

    if save:
        annotated = results[0].plot()
        cv2.imwrite("output.jpg", annotated)
        print("saved: output.jpg")

    if show:
        cv2.imshow("Detections", results[0].plot())
        cv2.waitKey(0)
        cv2.destroyAllWindows()


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python predict.py <image/video/stream>")
    else:
        detect(sys.argv[1])