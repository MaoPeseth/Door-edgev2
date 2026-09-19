import os
import cv2
import ultralytics as ut

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(SCRIPT_DIR, "yolov8n.pt")

print(f"Loading model from: {MODEL_PATH}")
model = ut.YOLO(MODEL_PATH)

CONF = 0.4
PERSON_CLASS = 1

cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("Cannot open camera")
    exit()

while True:
    ret, frame = cap.read()
    if not ret:
        break

    results = model(frame, conf=CONF, verbose=False)

    persons = [b for b in results[0].boxes if int(b.cls[0]) == PERSON_CLASS]
    annotated = results[0].plot()

    cv2.putText(
        annotated,
        f"Persons: {len(persons)}",
        (10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        1,
        (0, 255, 0),
        2,
    )
    cv2.imshow("Person Detection", annotated)

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

cap.release()
cv2.destroyAllWindows()