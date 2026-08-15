"""
Testing/send_enroll.py  —  test client for the mock Cloud server.

Sends one enrollment to the mock server (Testing/mock_cloud_server.py)
so the whole pipeline can be verified BEFORE wiring the real registration
software:  enroll → SSE push → SyncAgent pulls → Redis → display count.

Usage:
    python send_enroll.py --student-id STU-001 --name "Sok Dara" --card A1B2C3
    python send_enroll.py --student-id STU-002 --name "Chan Virak" \
        --embedding path/to/embedding.json
    python send_enroll.py --delete --student-id STU-001
"""
import argparse
import json
import random
import urllib.request

DEFAULT_URL = "http://localhost:5005"


def build_payload(args):
    payload = {
        "student_id": args.student_id,
        "name_en": args.name,
    }
    if args.card:
        payload["card_uid"] = args.card
    if args.embedding:
        emb = json.loads(open(args.embedding).read())
        payload["embedding"] = (emb.get("embedding") or emb.get("vector")
                                or emb.get("values") or emb)
    else:
        # Random 512-dim placeholder so the edge can index a test face.
        random.seed(hash(args.student_id) % 2**32)
        payload["embedding"] = [round(random.uniform(-1, 1), 6)
                                for _ in range(512)]
    return payload


def main():
    parser = argparse.ArgumentParser(description="Send a test enrollment "
                                                 "to the mock Cloud server")
    parser.add_argument("--url", default=DEFAULT_URL, help="mock server base URL")
    parser.add_argument("--student-id", required=True)
    parser.add_argument("--name", default="Test Student")
    parser.add_argument("--card", default="", help="card_uid (optional)")
    parser.add_argument("--embedding", default="", help="JSON file with a real "
                                                        "512-dim embedding")
    parser.add_argument("--delete", action="store_true",
                        help="delete this student instead of enrolling")
    args = parser.parse_args()

    endpoint = f"{args.url}/api/delete" if args.delete else f"{args.url}/api/enroll"
    body = {"student_id": args.student_id} if args.delete else build_payload(args)

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())
    print(f"[SendEnroll] {'DELETE' if args.delete else 'ENROLL'} "
          f"{args.student_id} → {result}")


if __name__ == "__main__":
    main()
