"""
Run once to get your Zoho refresh token.

Usage:
    python3 scripts/get_zoho_token.py

Steps before running:
  1. Go to https://api-console.zoho.com/
  2. Add Client → Self Client
  3. Generate a grant code with scope: ZohoBooks.fullaccess.all
  4. Paste your Client ID, Client Secret, and the grant code below when prompted.
"""

import urllib.request
import urllib.parse
import json


def get_tokens(client_id: str, client_secret: str, code: str, domain: str = "com") -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request(
        f"https://accounts.zoho.{domain}/oauth/v2/token",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def main():
    print("=== Zoho Books OAuth Token Generator ===\n")

    domain = input("Zoho domain (com / eu / in / com.au) [default: com]: ").strip() or "com"
    client_id = input("Client ID: ").strip()
    client_secret = input("Client Secret: ").strip()
    code = input("Grant Code (from api-console.zoho.com Self Client): ").strip()

    print("\nExchanging code for tokens...")
    try:
        result = get_tokens(client_id, client_secret, code, domain)
    except Exception as e:
        print(f"\nError: {e}")
        return

    print("\n" + "=" * 50)
    if "refresh_token" in result:
        print("SUCCESS! Add these to Vercel Environment Variables:\n")
        print(f"  ZOHO_CLIENT_ID      = {client_id}")
        print(f"  ZOHO_CLIENT_SECRET  = {client_secret}")
        print(f"  ZOHO_REFRESH_TOKEN  = {result['refresh_token']}")
        print(f"  ZOHO_DOMAIN         = {domain}")
        print()
        print(f"(Access token — expires in 1hr, not needed in Vercel: {result.get('access_token', '')})")
    else:
        print("Something went wrong. Full response:")
        print(json.dumps(result, indent=2))
    print("=" * 50)


if __name__ == "__main__":
    main()
