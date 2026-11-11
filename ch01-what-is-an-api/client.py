import sys

import httpx

BASE_URL = "http://127.0.0.1:8000"

book_id = sys.argv[1] if len(sys.argv) > 1 else "bk-1001"
response = httpx.get(f"{BASE_URL}/books/{book_id}")
response.raise_for_status()
book = response.json()
print(response.status_code, response.headers["content-type"])
print(f"{book['title']} by {book['author']}: "
      f"${book['price_cents'] / 100:.2f}, {book['in_stock']} in stock")
