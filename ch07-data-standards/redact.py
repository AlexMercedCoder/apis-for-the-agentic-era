"""Replace bearer tokens and storage secrets in a capture with
placeholders. Reads stdin, writes stdout."""
import re
import sys

text = sys.stdin.read()
text = re.sub(r"(Bearer )[A-Za-z0-9._\-]+", r"\1<token>", text)
for key in ("s3.secret-access-key", "s3.session-token", "access_token",
            "s3.access-key-id"):
    text = re.sub(rf'("{re.escape(key)}"\s*:\s*")[^"]+"',
                  rf'\1<{key}>"', text)
sys.stdout.write(text)
