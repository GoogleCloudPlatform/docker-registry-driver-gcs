import datetime
import functools
import logging
import tarfile
import tempfile
import time
import StringIO

import backports.lzma as lzma

import flask
import simplejson as json

import checksums
import storage
import toolkit
import rqueue
import cache

from .app import app
from .app import cfg
import storage.local


store = storage.load()
logger = logging.getLogger(__name__)


FILE_TYPES = {
    tarfile.REGTYPE: 'f',
    tarfile.DIRTYPE: 'd',
    tarfile.LNKTYPE: 'l',
    tarfile.SYMTYPE: 's',
    tarfile.CHRTYPE: 'c',
    tarfile.BLKTYPE: 'b',
}

# queue for requesting diff calculations from workers
diff_queue = rqueue.CappedCollection(cache.redis_conn, "diff-worker", 1024)

def require_completion(f):
    """This make sure that the image push correctly finished."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if store.exists(store.image_mark_path(kwargs['image_id'])):
            return toolkit.api_error('Image is being uploaded, retry later')
        return f(*args, **kwargs)
    return wrapper


def set_cache_headers(f):
    """Returns HTTP headers suitable for caching."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        # Set TTL to 1 year by default
        ttl = 31536000
        expires = datetime.datetime.fromtimestamp(int(time.time()) + ttl)
        expires = expires.strftime('%a, %d %b %Y %H:%M:%S GMT')
        headers = {
            'Cache-Control': 'public, max-age={0}'.format(ttl),
            'Expires': expires,
            'Last-Modified': 'Thu, 01 Jan 1970 00:00:00 GMT',
        }
        if 'If-Modified-Since' in flask.request.headers:
            return flask.Response(status=304, headers=headers)
        kwargs['headers'] = headers
        # Prevent the Cookie to be sent when the object is cacheable
        flask.session.modified = False
        return f(*args, **kwargs)
    return wrapper


def _get_image_layer(image_id, headers=None):
    if headers is None:
        headers = {}
    try:
        accel_uri_prefix = cfg.nginx_x_accel_redirect
        path = store.image_layer_path(image_id)
        if accel_uri_prefix:
            if isinstance(store, storage.local.LocalStorage):
                accel_uri = '/'.join([accel_uri_prefix, path])
                headers['X-Accel-Redirect'] = accel_uri
                logger.debug('send accelerated {0} ({1})'.format(
                    accel_uri, headers))
                return flask.Response('', headers=headers)
            else:
                logger.warn('nginx_x_accel_redirect config set,'
                            ' but storage is not LocalStorage')
        return flask.Response(store.stream_read(path), headers=headers)
    except IOError:
        return toolkit.api_error('Image not found', 404)


@app.route('/v1/private_images/<image_id>/layer', methods=['GET'])
@toolkit.requires_auth
@require_completion
def get_private_image_layer(image_id):
    try:
        repository = toolkit.get_repository()
        if not repository:
            # No auth token found, either standalone registry or privileged
            # access. In both cases, private images are "disabled"
            return toolkit.api_error('Image not found', 404)
        if not store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        return _get_image_layer(image_id)
    except IOError:
        return toolkit.api_error('Image not found', 404)


@app.route('/v1/images/<image_id>/layer', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_layer(image_id, headers):
    try:
        repository = toolkit.get_repository()
        if repository and store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        # If no auth token found, either standalone registry or privileged
        # access. In both cases, access is always "public".
        return _get_image_layer(image_id, headers)
    except IOError:
        return toolkit.api_error('Image not found', 404)


@app.route('/v1/images/<image_id>/layer', methods=['PUT'])
@toolkit.requires_auth
def put_image_layer(image_id):
    try:
        json_data = store.get_content(store.image_json_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    layer_path = store.image_layer_path(image_id)
    mark_path = store.image_mark_path(image_id)
    if store.exists(layer_path) and not store.exists(mark_path):
        return toolkit.api_error('Image already exists', 409)
    input_stream = flask.request.stream
    if flask.request.headers.get('transfer-encoding') == 'chunked':
        # Careful, might work only with WSGI servers supporting chunked
        # encoding (Gunicorn)
        input_stream = flask.request.environ['wsgi.input']
    # compute checksums
    csums = []
    sr = toolkit.SocketReader(input_stream)
    tmp, store_hndlr = storage.temp_store_handler()
    sr.add_handler(store_hndlr)
    h, sum_hndlr = checksums.simple_checksum_handler(json_data)
    sr.add_handler(sum_hndlr)
    store.stream_write(layer_path, sr)

    # read layer files and cache them
    try:
        files_json = json.dumps(_get_image_files_from_fobj(tmp))
        _set_image_files_cache(image_id, files_json)
    except Exception, e:
        logger.debug('put_image_layer: Error when caching layer file-tree:'
                     '{0}'.format(e))

    csums.append('sha256:{0}'.format(h.hexdigest()))
    try:
        tmp.seek(0)
        csums.append(checksums.compute_tarsum(tmp, json_data))
    except (IOError, checksums.TarError) as e:
        logger.debug('put_image_layer: Error when computing tarsum '
                     '{0}'.format(e))
    try:
        checksum = store.get_content(store.image_checksum_path(image_id))
    except IOError:
        # We don't have a checksum stored yet, that's fine skipping the check.
        # Not removing the mark though, image is not downloadable yet.
        flask.session['checksum'] = csums
        return toolkit.response()
    # We check if the checksums provided matches one the one we computed
    if checksum not in csums:
        logger.debug('put_image_layer: Wrong checksum')
        return toolkit.api_error('Checksum mismatch, ignoring the layer')

    tmp.close()

    # Checksum is ok, we remove the marker
    store.remove(mark_path)
    return toolkit.response()


@app.route('/v1/images/<image_id>/checksum', methods=['PUT'])
@toolkit.requires_auth
def put_image_checksum(image_id):
    checksum = flask.request.headers.get('X-Docker-Checksum')
    if not checksum:
        return toolkit.api_error('Missing Image\'s checksum')
    if not flask.session.get('checksum'):
        return toolkit.api_error('Checksum not found in Cookie')
    if not store.exists(store.image_json_path(image_id)):
        return toolkit.api_error('Image not found', 404)
    mark_path = store.image_mark_path(image_id)
    if not store.exists(mark_path):
        return toolkit.api_error('Cannot set this image checksum', 409)
    err = store_checksum(image_id, checksum)
    if err:
        return toolkit.api_error(err)
    if checksum not in flask.session.get('checksum', []):
        logger.debug('put_image_layer: Wrong checksum')
        return toolkit.api_error('Checksum mismatch')
    # Checksum is ok, we remove the marker
    store.remove(mark_path)
    return toolkit.response()


@app.route('/v1/private_images/<image_id>/json', methods=['GET'])
@toolkit.requires_auth
@require_completion
def get_private_image_json(image_id):
    repository = toolkit.get_repository()
    if not repository:
        # No auth token found, either standalone registry or privileged access
        # In both cases, private images are "disabled"
        return toolkit.api_error('Image not found', 404)
    try:
        if not store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        return _get_image_json(image_id)
    except IOError:
        return toolkit.api_error('Image not found', 404)


@app.route('/v1/images/<image_id>/json', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_json(image_id, headers):
    try:
        repository = toolkit.get_repository()
        if repository and store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        # If no auth token found, either standalone registry or privileged
        # access. In both cases, access is always "public".
        return _get_image_json(image_id, headers)
    except IOError:
        return toolkit.api_error('Image not found', 404)


def _get_image_json(image_id, headers=None):
    if headers is None:
        headers = {}
    try:
        data = store.get_content(store.image_json_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    try:
        size = store.get_size(store.image_layer_path(image_id))
        headers['X-Docker-Size'] = str(size)
    except OSError:
        pass
    checksum_path = store.image_checksum_path(image_id)
    if store.exists(checksum_path):
        headers['X-Docker-Checksum'] = store.get_content(checksum_path)
    return toolkit.response(data, headers=headers, raw=True)


@app.route('/v1/images/<image_id>/ancestry', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_ancestry(image_id, headers):
    try:
        data = store.get_content(store.image_ancestry_path(image_id))
    except IOError:
        return toolkit.api_error('Image not found', 404)
    return toolkit.response(json.loads(data), headers=headers)


def generate_ancestry(image_id, parent_id=None):
    if not parent_id:
        store.put_content(store.image_ancestry_path(image_id),
                          json.dumps([image_id]))
        return
    data = store.get_content(store.image_ancestry_path(parent_id))
    data = json.loads(data)
    data.insert(0, image_id)
    store.put_content(store.image_ancestry_path(image_id), json.dumps(data))


def check_images_list(image_id):
    full_repos_name = flask.session.get('repository')
    if not full_repos_name:
        # We only enforce this check when there is a repos name in the session
        # otherwise it means that the auth is disabled.
        return True
    try:
        path = store.images_list_path(*full_repos_name.split('/'))
        images_list = json.loads(store.get_content(path))
    except IOError:
        return False
    return (image_id in images_list)


def store_checksum(image_id, checksum):
    checksum_parts = checksum.split(':')
    if len(checksum_parts) != 2:
        return 'Invalid checksum format'
    # We store the checksum
    checksum_path = store.image_checksum_path(image_id)
    store.put_content(checksum_path, checksum)


@app.route('/v1/images/<image_id>/json', methods=['PUT'])
@toolkit.requires_auth
def put_image_json(image_id):
    try:
        data = json.loads(flask.request.data)
    except json.JSONDecodeError:
        pass
    if not data or not isinstance(data, dict):
        return toolkit.api_error('Invalid JSON')
    if 'id' not in data:
        return toolkit.api_error('Missing key `id\' in JSON')
    # Read the checksum
    checksum = flask.request.headers.get('X-Docker-Checksum')
    if checksum:
        # Storing the checksum is optional at this stage
        err = store_checksum(image_id, checksum)
        if err:
            return toolkit.api_error(err)
    else:
        # We cleanup any old checksum in case it's a retry after a fail
        store.remove(store.image_checksum_path(image_id))
    if image_id != data['id']:
        return toolkit.api_error('JSON data contains invalid id')
    if check_images_list(image_id) is False:
        return toolkit.api_error('This image does not belong to the '
                                 'repository')
    parent_id = data.get('parent')
    if parent_id and not store.exists(store.image_json_path(data['parent'])):
        return toolkit.api_error('Image depends on a non existing parent')
    json_path = store.image_json_path(image_id)
    mark_path = store.image_mark_path(image_id)
    if store.exists(json_path) and not store.exists(mark_path):
        return toolkit.api_error('Image already exists', 409)
    # If we reach that point, it means that this is a new image or a retry
    # on a failed push
    store.put_content(mark_path, 'true')
    store.put_content(json_path, flask.request.data)
    generate_ancestry(image_id, parent_id)
    return toolkit.response()


class layer_archive(object):
    '''
    Context manager for untaring a possibly xz/lzma compressed archive.
    '''
    def __init__(self, fobj):
        self.orig_fobj = fobj
        self.lzma_fobj = None
        self.tar_obj = None

    def __enter__(self):
        target_fobj = self.orig_fobj
        try: # try to decompress the archive
            self.lzma_fobj = lzma.LZMAFile(filename=target_fobj)
            self.lzma_fobj.read()
            self.lzma_fobj.seek(0)
        except lzma._lzma.LZMAError: 
            pass # its okay if we can't
        else: 
            target_fobj = self.lzma_fobj
        finally: # reset whatever fp we ended up using
            target_fobj.seek(0)

        # untar the fobj, whether it was the original or the lzma
        self.tar_obj = tarfile.open(mode='r|*', fileobj=target_fobj)
        return self.tar_obj

    def __exit__(self, type, value, traceback):
        # clean up
        self.tar_obj.close()
        self.lzma_fobj.close()
        self.orig_fobj.seek(0)

def _serialize_tar_info(tar_info):
    '''
    Take a single tarfile.TarInfo instance and serialize it to a
    tuple. Consider union whiteouts by filename and mark them as
    deleted in the third element. Don't include union metadata
    files.
    '''
    is_deleted = False
    filename = tar_info.name

    # notice and strip whiteouts
    if filename == ".":
        filename = '/'

    if filename.startswith("./"):
        filename = "/" + filename[2:]

    if filename.startswith("/.wh."):
        filename = "/" + filename[5:]
        is_deleted = True

    if filename.startswith("/.wh."):
        return None

    return (
        filename,
        FILE_TYPES[tar_info.type],
        is_deleted,
        tar_info.size,
        tar_info.mtime,
        tar_info.mode,
        tar_info.uid,
        tar_info.gid,
    )

def _read_tarfile(tar_fobj):
    # iterate over each file in the tar and then serialize it
    return [i for i in [_serialize_tar_info(m) for m in tar_fobj.getmembers()] if i is not None]

def _get_image_files_cache(image_id):
    image_files_path = store.image_files_path(image_id)
    if store.exists(image_files_path):
        return store.get_content(image_files_path)

def _set_image_files_cache(image_id, files_json):
    image_files_path = store.image_files_path(image_id)
    store.put_content(image_files_path, files_json)

def _get_image_files_from_fobj(layer_file):
    '''
    Download the specified layer and determine the file contents. Alternatively,
    process a passed in file-object containing the layer data.
    '''
    layer_file.seek(0)
    with layer_archive(layer_file) as tar_fobj:
        # read passed in tarfile directly
        files = _read_tarfile(tar_fobj)

    return files

def _get_image_files(image_id):
    '''
    Download the specified layer and determine the file contents. Alternatively,
    process a passed in file-object containing the layer data.
    '''
    files_json = _get_image_files_cache(image_id)
    if files_json:
        return files_json

    # download remote layer
    image_path = store.image_layer_path(image_id)
    with tempfile.TemporaryFile() as tmp_fobj:
        for buf in store.stream_read(image_path):
            tmp_fobj.write(buf)
        tmp_fobj.seek(0)
        # decompress and untar layer
        files_json = json.dumps(_get_image_files_from_fobj(tmp_fobj))
    _set_image_files_cache(image_id, files_json)
    return files_json

@app.route('/v1/private_images/<image_id>/files', methods=['GET'])
@toolkit.requires_auth
@require_completion
def get_private_image_files(image_id, headers):
    repository = toolkit.get_repository()
    if not repository:
        # No auth token found, either standalone registry or privileged access
        # In both cases, private images are "disabled"
        return toolkit.api_error('Image not found', 404)
    try:
        if not store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        data = _get_image_files(image_id)
        return toolkit.response(data, headers=headers, raw=True)
    except IOError:
        return toolkit.api_error('Image not found', 404)
    except tarfile.TarError:
        return toolkit.api_error('Layer format not supported', 400)


@app.route('/v1/images/<image_id>/files', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_files(image_id, headers):
    try:
        repository = toolkit.get_repository()
        if repository and store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)
        # If no auth token found, either standalone registry or privileged
        # access. In both cases, access is always "public".
        data = _get_image_files(image_id)
        return toolkit.response(data, headers=headers, raw=True)
    except IOError:
        return toolkit.api_error('Image not found', 404)
    except tarfile.TarError:
        return toolkit.api_error('Layer format not supported', 400)


def _get_file_info_map(file_infos):
    '''
    Convert a list of layer file info tuples to a dictionary using the 
    first element (filename) as the key.
    '''
    return dict((file_info[0], file_info[1:]) for file_info in file_infos)

def _get_image_diff_cache(image_id):
    image_diff_path = store.image_diff_path(image_id)
    if store.exists(image_diff_path):
        return store.get_content(image_diff_path)

def _set_image_diff_cache(image_id, diff_json):
    image_diff_path = store.image_diff_path(image_id)
    store.put_content(image_diff_path, diff_json)

def _get_image_diff(image_id):
    '''
    Calculate the diff information for the files contained within
    the layer. Return a dictionary of lists grouped by whether they
    were deleted, changed or created in this layer.

    To determine what happened to a file in a layer we walk backwards
    through the ancestry until we see the file in an older layer. Based
    on whether the file was previously deleted or not we know whether
    the file was created or modified. If we do not find the file in an
    ancestor we know the file was just created.

        - File marked as deleted by union fs tar: DELETED
        - Ancestor contains non-deleted file:     CHANGED
        - Ancestor contains deleted marked file:  CREATED
        - No ancestor contains file:              CREATED
    '''

    # check the cache first
    diff_json = _get_image_diff_cache(image_id)
    if diff_json:
        return diff_json

    # we need all ancestral layers to calculate the diff
    ancestry = json.loads(store.get_content(store.image_ancestry_path(image_id)))[1:]
    # grab the files from the layer
    files = json.loads(_get_image_files(image_id))
    # convert to a dictionary by filename
    info_map = _get_file_info_map(files)

    deleted = {}
    changed = {}
    created = {}

    # walk backwards in time by iterating the ancestry
    for id in ancestry:
        # get the files from the current ancestor
        ancestor_files = json.loads(_get_image_files(id))
        # convert to a dictionary of the files mapped by filename
        ancestor_map = _get_file_info_map(ancestor_files)
        # iterate over each of the top layer's files
        for filename, info in info_map.items():
            ancestor_info = ancestor_map.get(filename)
            # if the file in the top layer is already marked as deleted
            if info[1]:
                deleted[filename] = info
                del info_map[filename]
            # if the file exists in the current ancestor
            elif ancestor_info:
                # if the file was marked as deleted in the ancestor
                if ancestor_info[1]:
                    # is must have been just created in the top layer
                    created[filename] = info
                else:
                    # otherwise it must have simply changed in the top layer
                    changed[filename] = info
                del info_map[filename]
    created.update(info_map)

    # return dictionary of files grouped by file action
    diff_json = json.dumps({
        'deleted': deleted,
        'changed': changed,
        'created': created,
    })

    # store results in cache
    _set_image_diff_cache(image_id, diff_json)

    return diff_json

@app.route('/v1/images/<image_id>/diff', methods=['GET'])
@toolkit.requires_auth
@require_completion
@set_cache_headers
def get_image_diff(image_id, headers):
    try:
        repository = toolkit.get_repository()
        if repository and store.is_private(*repository):
            return toolkit.api_error('Image not found', 404)

        # first try the cache
        diff_json = _get_image_diff_cache(image_id)
        # it the cache misses, request a diff from a worker
        if not diff_json:
            diff_queue.push(image_id)
            # empty response - #FIXME use http code 202 or 503
            diff_json = ""


        return toolkit.response(diff_json, headers=headers, raw=True)
    except IOError:
        return toolkit.api_error('Image not found', 404)
    except tarfile.TarError:
        return toolkit.api_error('Layer format not supported', 400)

