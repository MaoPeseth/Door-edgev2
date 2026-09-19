import cv2
import ultralytics as ut

model = ut.YOLO("yolov8n.pt")

CONF = 0.4
PERSON_CLASS = 0


def main():
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        print("Cannot open camera")
        return

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        results = model(frame, conf=CONF, classes=[PERSON_CLASS], verbose=False)
        annotated = results[0].plot()

        for box in results[0].boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
            conf = float(box.conf[0])
            print(f"person {conf:.2f} [{x1},{y1},{x2},{y2}]")

        cv2.imshow("Person Detection", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()