import json
from gateway.platforms.api_server import _build_hermes_code_model_pool, _build_hermes_privacy_model_pool
code = _build_hermes_code_model_pool()
priv = _build_hermes_privacy_model_pool()
print("CODE_COUNT=" + str(len(code)))
for m in code:
    print("CODE\t" + m)
print("PRIVACY_COUNT=" + str(len(priv)))
for m in priv:
    print("PRIVACY\t" + m)
