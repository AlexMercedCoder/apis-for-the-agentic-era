"""Chapter 10's Harbor MCP server, unchanged, behind the gateway.

Three settings move, and the token check also requires exp, iat, iss,
aud, and sub: tokens now come from this chapter's test issuer,
the resource (the audience a token must carry) is the gateway's public URL
for the server, and the metadata URL is the gateway's too. The tools, the
argument validation, the step-up, and the confirmations are Chapter 10's.

    uv run python mcp_server.py           # 127.0.0.1:8010, reached via :9080
"""

import sys
from pathlib import Path

import jwt
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                       / "ch10-agent-protocols"))
import harbor_mcp  # noqa: E402

REQUIRED = ["exp", "iat", "iss", "aud", "sub"]    # Chapter 8's list


class StrictVerifier(harbor_mcp.KeycloakVerifier):
    """Chapter 10's checks, plus: refuse a token missing a required claim.

    PyJWT checks exp only when a token carries it; this makes it required.
    """

    async def verify_token(self, token):
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None
        if any(name not in claims for name in REQUIRED):
            harbor_mcp.log.info("token refused: missing a required claim")
            return None
        return await super().verify_token(token)


harbor_mcp.KeycloakVerifier = StrictVerifier
harbor_mcp.ISSUER = "http://127.0.0.1:8110"
harbor_mcp.RESOURCE = "http://127.0.0.1:9080/mcp"
harbor_mcp.PRM_URL = ("http://127.0.0.1:9080/.well-known/"
                      "oauth-protected-resource/mcp")

if __name__ == "__main__":
    uvicorn.run(harbor_mcp.app(), host="127.0.0.1", port=8010,
                log_level="warning")
