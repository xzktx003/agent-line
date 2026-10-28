#!/usr/bin/env python3
"""Import pinned public images using the shell's HTTPS proxy.

This never changes Docker daemon networking. All downloaded manifests, configs
and layers must match the pinned SHA256 digests before Docker receives them.
"""

import concurrent.futures
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request

IMAGES = {
    "redis": ("registry-1.docker.io", "library/redis", "7aec734b2bb298a1d769fd8729f13b8514a41bf90fcdd1f38ec52267fbaa8ee6"),
    "postgres": ("registry-1.docker.io", "pgvector/pgvector", "feb68f4f15446397d8cac7f4fe48fe4586de83160d1fc48b46283312d1a33966"),
    "simstudio": ("ghcr.io", "simstudioai/simstudio", "41eb452d1552ae465fa71afa5fac57ed6590fb05bbb9f7a330de14375b1a9668"),
    "realtime": ("ghcr.io", "simstudioai/realtime", "3059e650bac964d3099679aef0bb14c380a53a073faee88cd8fcfb3352e83e5f"),
    "migrations": ("ghcr.io", "simstudioai/migrations", "c2cff1627f5d79d4c96d17f0ed9550ea0046b07f06d6466a9ab2bc44e7565fd2"),
}
ACCEPT = ",".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))


def open_with_retry(request, timeout):
    for attempt in range(5):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)


def main():
    name = sys.argv[1]
    registry, repo, index_sha = IMAGES[name]
    root = Path(__file__).resolve().parents[2]
    legacy_cache = root / 'var/sim/image-cache'
    # First-image preparation must not make a fresh installation look existing.
    cache = legacy_cache if legacy_cache.is_dir() else root / 'var/image-cache/sim'
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_url = (
        f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
        if registry == "registry-1.docker.io" else
        f"https://ghcr.io/token?scope=repository:{repo}:pull"
    )
    with open_with_retry(token_url, timeout=45) as response:
        token = json.load(response)["token"]

    def download(kind, digest):
        sha = digest.removeprefix("sha256:")
        target = cache / sha
        if target.exists():
            with target.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() == sha:
                    return target
        request = urllib.request.Request(
            f"https://{registry}/v2/{repo}/{kind}/{digest}",
            headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT},
        )
        temporary = target.with_suffix(".partial")
        checksum = hashlib.sha256()
        with open_with_retry(request, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                checksum.update(chunk)
        if checksum.hexdigest() != sha:
            temporary.unlink()
            raise ValueError(f"Digest mismatch for {digest}")
        temporary.replace(target)
        print(f"Verified {kind} {digest}", flush=True)
        return target

    index = json.loads(download("manifests", "sha256:" + index_sha).read_bytes())
    if "manifests" in index:
        entry = next(item for item in index["manifests"] if item.get("platform", {}) == {"architecture": "amd64", "os": "linux"})
        manifest = json.loads(download("manifests", entry["digest"]).read_bytes())
    else:
        manifest = index
    config = download("blobs", manifest["config"]["digest"])
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        layers = list(pool.map(lambda layer: download("blobs", layer["digest"]), manifest["layers"]))
    tag = f"agent-line/{name}:sha256-{index_sha[:16]}"
    archive = cache / f"{name}.tar"
    with tarfile.open(archive, "w") as output:
        output.add(config, arcname=config.name + ".json")
        for layer in layers:
            output.add(layer, arcname=layer.name + ".tar.gz")
        data = json.dumps([{"Config": config.name + ".json", "RepoTags": [tag], "Layers": [layer.name + ".tar.gz" for layer in layers]}]).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(data)
        output.addfile(member, io.BytesIO(data))
    subprocess.run(["docker", "load", "-i", str(archive)], check=True)
    archive.unlink()
    print(f"Imported {tag} from verified index sha256:{index_sha}")


if __name__ == "__main__":
    main()
