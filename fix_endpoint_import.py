# Read the endpoints.py file
with open('tusker_gateway/endpoints.py', 'r') as f:
    lines = f.readlines()

# Add the missing _resolve_api_key function at the end of the file
resolve_api_key_func = '''

def _resolve_api_key(request: web.Request) -> str:
    """Return the raw bearer token used by the caller (for budget keying)."""
    return (request.headers.get("authorization") or request.headers.get("x-api-key") or "").strip()
'''

# Insert it before the imports if not present
import_end = None
for i, line in enumerate(lines):
    if 'from tusker_gateway.endpoints import' in line:
        import_end = i
        break

if import_end:
    lines.insert(import_end, resolve_api_key_func)
    print("✅ Added _resolve_api_key function")
else:
    print("❌ Could not find import section")

# Write back
with open('tusker_gateway/endpoints.py', 'w') ofile = open('tusker_gateway/endpoints.py', 'w') ofile.writelines(lines)
print("✅ endpoints.py updated with _resolve_api_key function")
