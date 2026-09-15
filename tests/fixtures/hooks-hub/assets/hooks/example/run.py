import json
import sys

if __name__ == "__main__":
    event = json.load(sys.stdin)
    print(json.dumps({"context": event["event"]}))
