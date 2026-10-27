import os

database_url = os.environ["DATABASE_URL"]
api_token = os.environ["API_TOKEN"]
debug = os.getenv("DEBUG", "0") == "1"

print(f"connecting to {database_url} (debug={debug})")
