#!/bin/bash
# Shared image-provenance lookups for k8s/deploy.sh and k8s/verify-provenance.sh.
#
# The registry is internal and its TLS certificate is not publicly trusted, so
# curl skips verification unless PROV_REGISTRY_CACERT names a CA bundle.
# Manifests are stored in the OCI format, which the registry only serves when the
# OCI media type is in Accept; without it the registry answers 404 with
# "OCI manifest found, but accept header does not support OCI manifests".
# The registry name resolves only inside the cluster network, so a workstation
# run falls back to asking PROV_REGISTRY_SSH (e.g. visor) to do the lookup.
#
# Overridables: PROV_REGISTRY, PROV_REGISTRY_SCHEME, PROV_REPO,
#               PROV_REGISTRY_CACERT, PROV_REGISTRY_SSH.

PROV_REGISTRY=${PROV_REGISTRY:-registry.tusker.net.au:5000}
PROV_REGISTRY_SCHEME=${PROV_REGISTRY_SCHEME:-https}
PROV_REPO=${PROV_REPO:-tusker-gateway}
PROV_ACCEPT="application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json"

prov_curl_tls() {
    if [[ -n "${PROV_REGISTRY_CACERT:-}" ]]; then
        echo "--cacert ${PROV_REGISTRY_CACERT}"
    else
        echo "--insecure"
    fi
}

prov_extract_digest() {
    tr -d '\r' | awk -F': ' 'tolower($1)=="docker-content-digest"{print $2}' | tail -1
}

# prov_registry_digest <tag | sha256:hex>
#   Prints the digest the registry reports for that reference, or returns 1 when
#   the registry does not know it (a 404 is not an error worth printing).
prov_registry_digest() {
    local ref=$1 digest remote_cmd
    digest=$(curl -s -m 10 -o /dev/null -D - -H "Accept: ${PROV_ACCEPT}" $(prov_curl_tls) \
        "${PROV_REGISTRY_SCHEME}://${PROV_REGISTRY}/v2/${PROV_REPO}/manifests/${ref}" 2>/dev/null \
        | prov_extract_digest) || true

    if [[ ! "${digest}" =~ ^sha256:[0-9a-f]{64}$ && -n "${PROV_REGISTRY_SSH:-}" ]]; then
        remote_cmd="curl -s -m 10 -o /dev/null -D - -H 'Accept: ${PROV_ACCEPT}' $(prov_curl_tls) \
'${PROV_REGISTRY_SCHEME}://${PROV_REGISTRY}/v2/${PROV_REPO}/manifests/${ref}'"
        digest=$(ssh -o ConnectTimeout=8 "${PROV_REGISTRY_SSH}" "${remote_cmd}" 2>/dev/null \
            | prov_extract_digest) || true
    fi

    if [[ "${digest}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        printf '%s\n' "${digest}"
        return 0
    fi
    return 1
}

# prov_image_digest <image-ref>
#   An immutable reference (repo@sha256:...) is returned as-is. A tag is resolved
#   through the registry, so a tag that was re-pushed cannot masquerade as the
#   binary that was originally published under it.
prov_image_digest() {
    local ref=$1
    if [[ "${ref}" == *@sha256:* ]]; then
        printf '%s\n' "${ref##*@}"
        return 0
    fi
    prov_registry_digest "${ref##*:}"
}