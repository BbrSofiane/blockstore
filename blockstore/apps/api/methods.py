"""
API Client methods for working with Blockstore bundles and drafts
"""

import base64
from urllib.parse import urlencode
from uuid import UUID

import dateutil.parser
from django.core.exceptions import ImproperlyConfigured
import requests
import six

from blockstore.apps.bundles import models
from blockstore.apps.bundles.links import LinkCycleError
from blockstore.apps.bundles.store import DraftRepo, SnapshotRepo
from blockstore.apps.rest_api.v1.serializers.drafts import (
    DraftFileUpdateSerializer,
    DraftSerializer,
    DraftWithFileDataSerializer,
)

from .data import (
    BundleData,
    CollectionData,
    DraftData,
    BundleFileData,
    DraftFileData,
    LinkDetailsData,
    LinkReferenceData,
    DraftLinkDetailsData,
)
from .exceptions import (
    NotFound,
    CollectionNotFound,
    BundleNotFound,
    DraftNotFound,
    BundleFileNotFound,
)


def _collection_data_from_model(collection_model):
    """
    Create and return CollectionData from collection model.
    """
    return CollectionData(uuid=collection_model.uuid, title=collection_model.title)


def _get_collection_model(collection_uuid):
    """
    Get collection model from UUID.

    Raises CollectionNotFound if the collection does not exist.
    """
    try:
        collection_model = models.Collection.objects.get(uuid=collection_uuid)
    except models.Collection.DoesNotExist:
        raise CollectionNotFound("Collection {} does not exist.".format(collection_uuid))
    return collection_model


def get_collection(collection_uuid):
    """
    Retrieve data about the specified collection.

    Raises CollectionNotFound if collection with UUID does not exist.
    """
    collection_model = _get_collection_model(collection_uuid)
    return _collection_data_from_model(collection_model)


def create_collection(title):
    """
    Create a new collection.
    """
    collection_model = models.Collection(title=title)
    collection_model.save()
    return _collection_data_from_model(collection_model)


def update_collection(collection_uuid, title):
    """
    Update a collection's title.
    """
    collection_model = _get_collection_model(collection_uuid)
    collection_model.title = title
    collection_model.save()
    return _collection_data_from_model(collection_model)


def delete_collection(collection_uuid):
    """
    Delete a collection.
    """
    collection_model = _get_collection_model(collection_uuid)
    collection_model.delete()


def _bundle_data_from_model(bundle_model):
    """
    Create and return BundleData from bundle model.
    """
    latest_bundle_version_model = bundle_model.get_bundle_version()
    return BundleData(
        uuid=bundle_model.uuid,
        title=bundle_model.title,
        description=bundle_model.description,
        slug=bundle_model.slug,
        drafts={draft.name: draft.uuid for draft in bundle_model.drafts.all()},
        latest_version=latest_bundle_version_model.version_num if latest_bundle_version_model else 0,
    )


def _get_bundle_model(bundle_uuid):
    """
    Get Bundle model from UUID.

    Raises BundleNotFound if bundle with UUID does not exist.
    """
    try:
        bundle_model = models.Bundle.objects.get(uuid=bundle_uuid)
    except models.Bundle.DoesNotExist:
        raise BundleNotFound("Bundle {} does not exist.".format(bundle_uuid))
    return bundle_model


def get_bundles(uuids=None, text_search=None):
    """
    Get the details of all bundles.
    """
    query_params = {}
    if uuids:
        query_params['uuid'] = ','.join(map(str, uuids))
    if text_search:
        query_params['text_search'] = text_search
    version_url = api_url('bundles') + '?' + urlencode(query_params)
    response = api_request('get', version_url)
    # build bundle from response, convert map object to list and return
    return [_bundle_from_response(item) for item in response]


def get_bundle(bundle_uuid):
    """
    Retrieve data about the specified bundle.

    Raises BundleNotFound if bundle with UUID does not exist.
    """
    bundle_model = _get_bundle_model(bundle_uuid)
    return _bundle_data_from_model(bundle_model)


def create_bundle(collection_uuid, slug, title="New Bundle", description=""):
    """
    Create a new bundle.

    Note that description is currently required.
    """
    collection_model = _get_collection_model(collection_uuid)
    bundle_model = models.Bundle(
        title=title,
        collection=collection_model,
        slug=slug,
        description=description,
    )
    bundle_model.save()
    return _bundle_data_from_model(bundle_model)


def update_bundle(bundle_uuid, **fields):
    """
    Update a bundle's title, description, slug, or collection.
    """
    bundle_model = _get_bundle_model(bundle_uuid)
    data = {}
    # TODO: Add validation.
    for str_field in ("title", "description", "slug"):
        if str_field in fields:
            setattr(bundle_model, str_field, fields.pop(str_field))
    if "collection_uuid" in fields:
        collection_uuid = fields.pop("collection_uuid")
        collection_model = _get_collection_model(collection_uuid)
        bundle_model.collection = collection_model
    if fields:
        raise ValueError("Unexpected extra fields passed to update_bundle: {}".format(fields.keys()))

    bundle_model.save()
    return _bundle_data_from_model(bundle_model)


def delete_bundle(bundle_uuid):
    """
    Delete a bundle.
    """
    bundle_model = _get_bundle_model(bundle_uuid)
    bundle_model.delete()


def _draft_data_from_model(draft_model):
    """
    Create and return DraftData from draft model.
    """
    return DraftData(
        uuid=draft_model.uuid,
        bundle_uuid=draft_model.bundle.uuid,
        name=draft_model.name,
        updated_at=draft_model.staged_draft.updated_at,
        files={
            path: DraftFileData(
                path=path,
                size=file_info.size,
                url=path,  ## Todo
                hash_digest=file_info.hash_digest,
                modified=path in draft.staged_draft.files_to_overwrite,
            )
            for path, file_info in draft_model.staged_draft.files.items()
        },
        links={}
        # Todo
        # links={
        #     name: DraftLinkDetails(
        #         name=name,
        #         direct=LinkReference(**link["direct"]),
        #         indirect=[LinkReference(**ind) for ind in link["indirect"]],
        #         modified=link["modified"],
        #     )
        #     for name, link in draft.staged_draft.composed_links.items()
        # }
    )


def _get_draft_model(draft_uuid):
    """
    Get Draft model from UUID.

    Raises DraftNotFound if draft with UUID does not exist.
    """
    try:
        draft_model = models.Draft.objects.get(uuid=draft_uuid)
    except models.Draft.DoesNotExist:
        raise DraftNotFound("Draft does not exist: {}".format(draft_uuid))
    return draft_model


def get_draft(draft_uuid):
    """
    Retrieve data about the specified draft.

    If you don't know the draft's UUID, look it up using get_bundle()
    """
    draft_model = _get_draft_model(draft_uuid)
    return _draft_data_from_model(draft_model)


def get_or_create_bundle_draft(bundle_uuid, draft_name):
    """
    Retrieve data about the specified draft, creating a new one if it does not exist yet.
    """
    try:
        draft_model = models.Draft.objects.get(bundle__uuid=bundle_uuid, name=draft_name)
    except models.Draft.DoesNotExist:
        # The draft doesn't exist yet, so create it:
        bundle_model = _get_bundle_model(bundle_uuid)
        draft_model = models.Draft(
            bundle=bundle_model,
            name=draft_name,
        )
        draft_model.save()
    return _draft_data_from_model(draft_model)


def commit_draft(draft_uuid):
    """
    Commit all of the pending changes in the draft, creating a new version of
    the associated bundle.

    Does not return any value.
    """
    draft_repo = DraftRepo(SnapshotRepo())
    staged_draft = draft_repo.get(draft_uuid)

    # TODO: Is this the appropriate response when trying to commit a Draft with
    # no changes?
    if not staged_draft.files_to_overwrite and not staged_draft.links_to_overwrite:
        raise serializers.ValidationError("Draft has no changes to commit.")

    new_snapshot, _updated_draft = draft_repo.commit(staged_draft)
    new_bundle_version = models.BundleVersion.create_new_version(
        new_snapshot.bundle_uuid, new_snapshot.hash_digest
    )
    return new_bundle_version


def delete_draft(draft_uuid):
    """
    Delete the specified draft, removing any staged changes/files/deletes.

    Does not return any value.
    """
    draft_repo = DraftRepo(SnapshotRepo())
    draft_repo.delete(draft_uuid)
    draft_model = _get_draft_model(draft_uuid)
    draft_model.delete()


def _get_bundle_version_model(bundle_uuid, version_number=None):
    """
    Get BundleVersion from bundle UUID and version number.

    If version_number is None, returns the latest bundle version of the bundle.
    """
    return models.BundleVersion.get_bundle_version(bundle_uuid=bundle_uuid, version_num=version_number)


def _bundle_version_data_from_model(draft):
    """
    :param draft:
    :return:
    """
    snapshot = bundle_version_model.snapshot()
    snapshot_repo = SnapshotRepo()

    # TODO

    files = (
        BundleFileData(
            path=path,
            url=snapshot_repo.url(snapshot, path),
            size=file_info.size,
            hash_digest=file_info.hash_digest.hex(),
        ) for path, file_metadata in snapshot.files.items()
    )

    # info['links'] = {
    #     link.name: {
    #         "direct": self._serialized_dep(link.direct_dependency),
    #         "indirect": [
    #             self._serialized_dep(dep)
    #             for dep in link.indirect_dependencies
    #         ]
    #     }
    #     for link in snapshot.links
    # }

    return {
        link.name: LinkDetailsData(
            name=link.name,
            direct=LinkReferenceData(**link["direct"]),
            indirect=[LinkReferenceData(**ind) for ind in link["indirect"]],
        )
        for link in snapshot.links

    }
    # return BundleVersionWithFileDataSerializer


def get_bundle_version(bundle_uuid, version_number): ### ??
    """
    Get the details of the specified bundle version
    """
    bundle_version_model = _get_bundle_version_model(bundle_uuid, version_number)
    if bundle_version_model is None:
        pass

def get_bundle_version_files(bundle_uuid, version_number=None):
    """
    Get a list of the files in the specified bundle version
    """
    bundle_version_model = _get_bundle_version_model(bundle_uuid, version_number)
    if bundle_version_model is None:
        return ()
    return _bundle_version_data_from_model(bundle_version_model).files


def get_bundle_version_links(bundle_uuid, version_number=None):
    """
    Get a dictionary of the links in the specified bundle version
    """
    bundle_version_model = _get_bundle_version_model(bundle_uuid, version_number)
    if bundle_version_model is None:
        return {}
    return _bundle_version_data_from_model(bundle_version_model).links


def get_bundle_files_dict(bundle_uuid, use_draft=None):
    """
    Get a dict of all the files in the specified bundle.

    Returns a dict where the keys are the paths (strings) and the values are
    BundleFileData or DraftFileData tuples.
    """
    if use_draft:
        try:
            draft_model = models.Draft.objects.get(bundle__uuid=bundle_uuid, name=use_draft)
        except models.Draft.DoesNotExist:
            raise DraftNotFound("Draft for Bundle {} with name {} does not exist.".format(bundle_uuid, use_draft))
        return _draft_data_from_model(draft_model).files

    return {file_meta.path: file_meta for file_meta in get_bundle_version_files(bundle_uuid)}


def get_bundle_files(bundle_uuid, use_draft=None):
    """
    Get an iterator over all the files in the specified bundle or draft.
    """
    return get_bundle_files_dict(bundle_uuid, use_draft).values()


def get_bundle_links(bundle_uuid, use_draft=None):
    """
    Get a dict of all the links in the specified bundle.

    Returns a dict where the keys are the link names (strings) and the values
    are LinkDetails or DraftLinkDetails tuples.
    """
    if use_draft:
        try:
            draft_model = models.Draft.objects.get(bundle__uuid=bundle_uuid, name=use_draft)
        except models.Draft.DoesNotExist:
            raise DraftNotFound("Draft for Bundle {} with name {} does not exist.".format(bundle_uuid, use_draft))
        return _draft_data_from_model(draft_model).links

    return get_bundle_version_links(bundle_uuid)


def get_bundle_file_metadata(bundle_uuid, path, use_draft=None):
    """
    Get the metadata of the specified file.
    """
    files_dict = get_bundle_files_dict(bundle_uuid, use_draft=use_draft)
    try:
        return files_dict[path]
    except KeyError:
        raise BundleFileNotFound(
            "Bundle {} (draft: {}) does not contain a file {}".format(bundle_uuid, use_draft, path)
        )


def get_bundle_file_data(bundle_uuid, path, use_draft=None):
    """
    Read all the data in the given bundle file and return it as a
    binary string.

    Do not use this for large files!
    """
    metadata = get_bundle_file_metadata(bundle_uuid, path, use_draft)
    with requests.get(metadata.url, stream=True) as r:
        return r.content


def write_draft_file(draft_uuid, path, contents):
    """
    Create or overwrite the file at 'path' in the specified draft with the given
    contents. To delete a file, pass contents=None.

    If you don't know the draft's UUID, look it up using
    get_or_create_bundle_draft()

    Does not return anything.
    """
    data = {
        'files': {
            path: encode_str_for_draft(contents) if contents is not None else None,
        },
    }
    serializer = DraftFileUpdateSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    files_to_write = serializer.validated_data['files']
    dependencies_to_write = serializer.validated_data['links']

    draft_repo = DraftRepo(SnapshotRepo())
    try:
        draft_repo.update(draft_uuid, files_to_write, dependencies_to_write)
    except LinkCycleError:
        raise serializers.ValidationError("Link cycle detected: Cannot create draft.")


def set_draft_link(draft_uuid, link_name, bundle_uuid, version):
    """
    Create or replace the link with the given name in the specified draft so
    that it points to the specified bundle version. To delete a link, pass
    bundle_uuid=None, version=None.

    If you don't know the draft's UUID, look it up using
    get_or_create_bundle_draft()

    Does not return anything.
    """
    api_request('patch', api_url('drafts', str(draft_uuid)), json={
        'links': {
            link_name: {"bundle_uuid": str(bundle_uuid), "version": version} if bundle_uuid is not None else None,
        },
    })


def encode_str_for_draft(input_str):
    """
    Given a string, return UTF-8 representation that is then base64 encoded.
    """
    if isinstance(input_str, six.text_type):
        binary = input_str.encode('utf8')
    else:
        binary = input_str
    return base64.b64encode(binary)
