"""Link Docker classic and containerd IDs to the same shipped image content.

Runs during packaging, never on the customer's host. Do not infer a trusted ID
from a tag or from the customer's Docker output.
"""
import hashlib
import json
import re
import tarfile

DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')


def image_ids(path, tag, recorded_id, arch):
    with tarfile.open(path, 'r:gz') as archive:
        names = archive.getnames()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate image archive members')

        def read(name, descriptor=None):
            entry = archive.getmember(name)
            if not entry.isfile() or entry.size > 2 * 1024 * 1024:
                raise ValueError('Invalid image metadata member')
            data = archive.extractfile(entry).read()
            if descriptor is not None:
                if (descriptor.get('size') != len(data)
                        or descriptor.get('digest') != 'sha256:' + hashlib.sha256(data).hexdigest()):
                    raise ValueError('Image descriptor hash or size mismatch')
            return json.loads(data)

        def blob(descriptor):
            digest = descriptor.get('digest', '')
            if not DIGEST.fullmatch(digest):
                raise ValueError('Invalid image descriptor digest')
            return 'blobs/sha256/' + digest.split(':')[1]

        index = read('index.json')
        if index.get('schemaVersion') != 2 or len(index.get('manifests', [])) != 1:
            raise ValueError('Expected one image manifest per architecture')
        descriptor = index['manifests'][0]
        if descriptor.get('mediaType') != 'application/vnd.oci.image.manifest.v1+json':
            raise ValueError('Unsupported release image manifest type')
        manifest = read(blob(descriptor), descriptor)
        if (manifest.get('schemaVersion') != 2
                or manifest.get('mediaType') != descriptor['mediaType']):
            raise ValueError('Image manifest type mismatch')
        config_descriptor = manifest['config']
        if config_descriptor.get('mediaType') != 'application/vnd.oci.image.config.v1+json':
            raise ValueError('Unsupported release image configuration type')
        config_path = blob(config_descriptor)
        config = read(config_path, config_descriptor)
        if config.get('os') != 'linux' or config.get('architecture') != arch:
            raise ValueError('Release image platform mismatch')
        legacy = read('manifest.json')
        if (len(legacy) != 1 or legacy[0].get('RepoTags') != [tag]
                or legacy[0].get('Config') != config_path
                or legacy[0].get('Layers') != [blob(layer) for layer in manifest['layers']]):
            raise ValueError('Docker and OCI image records disagree')
        ids = {'config': config_descriptor['digest'], 'manifest': descriptor['digest']}
        if recorded_id not in ids.values():
            raise ValueError('Image archive does not match the tested image ID')
        return ids
