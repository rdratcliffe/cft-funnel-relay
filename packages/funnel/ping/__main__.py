import json
def main(event):
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"}, "body": json.dumps({"pong": True, "keys": sorted(list(event.keys()))[:15]})}
