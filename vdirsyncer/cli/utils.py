from __future__ import annotations

import contextlib
import errno
import importlib
import json
import os
import sys
from typing import Any

import aiohttp
import click

from .. import BUGTRACKER_HOME
from .. import DOCS_HOME
from .. import exceptions
from ..storage.base import Storage
from ..sync.exceptions import IdentConflict
from ..sync.exceptions import PartialSync
from ..sync.exceptions import StorageEmpty
from ..sync.exceptions import SyncConflict
from ..sync.status import SqliteStatus
from ..utils import atomic_write
from ..utils import expand_path
from ..utils import get_storage_init_args
from . import cli_logger

STATUS_PERMISSIONS = 0o600
STATUS_DIR_PERMISSIONS = 0o700


class _StorageIndex:
    def __init__(self):
        self._storages: dict[str, str] = {
            "caldav": "vdirsyncer.storage.dav.CalDAVStorage",
            "carddav": "vdirsyncer.storage.dav.CardDAVStorage",
            "filesystem": "vdirsyncer.storage.filesystem.FilesystemStorage",
            "http": "vdirsyncer.storage.http.HttpStorage",
            "singlefile": "vdirsyncer.storage.singlefile.SingleFileStorage",
            "google_calendar": "vdirsyncer.storage.google.GoogleCalendarStorage",
            "google_contacts": "vdirsyncer.storage.google.GoogleContactsStorage",
        }

    def __getitem__(self, name: str) -> Storage:
        item = self._storages[name]
        if not isinstance(item, str):
            return item

        modname, clsname = item.rsplit(".", 1)
        mod = importlib.import_module(modname)
        self._storages[name] = rv = getattr(mod, clsname)
        assert rv.storage_name == name
        return rv


storage_names = _StorageIndex()
del _StorageIndex


class JobFailed(RuntimeError):
    pass


def handle_cli_error(status_name=None, e=None):
    """
    Print a useful error message for the current exception.

    This is supposed to catch all exceptions, and should never raise any
    exceptions itself.
    """

    try:
        if e is not None:
            raise e
        else:
            raise
    except exceptions.UserError as e:
        cli_logger.critical(e)
    except StorageEmpty as e:
        cli_logger.error(
            '{status_name}: Storage "{name}" was completely emptied. If you '
            "want to delete ALL entries on BOTH sides, then use "
            "`vdirsyncer sync --force-delete {status_name}`. "
            "Otherwise delete the files for {status_name} in your status "
            "directory.".format(
                name=e.empty_storage.instance_name, status_name=status_name
            )
        )
    except PartialSync as e:
        cli_logger.error(
            f"{status_name}: Attempted change on {e.storage}, which is read-only"
            ". Set `partial_sync` in your pair section to `ignore` to ignore "
            "those changes, or `revert` to revert them on the other side."
        )
    except SyncConflict as e:
        cli_logger.error(
            f"{status_name}: One item changed on both sides. Resolve this "
            "conflict manually, or by setting the `conflict_resolution` "
            "parameter in your config file.\n"
            f"See also {DOCS_HOME}/config.html#pair-section\n"
            f"Item ID: {e.ident}\n"
            f"Item href on side A: {e.href_a}\n"
            f"Item href on side B: {e.href_b}\n"
        )
    except IdentConflict as e:
        cli_logger.error(
            '{status_name}: Storage "{storage.instance_name}" contains '
            "multiple items with the same UID or even content. Vdirsyncer "
            "will now abort the synchronization of this collection, because "
            "the fix for this is not clear; It could be the result of a badly "
            "behaving server. You can try running:\n\n"
            "    vdirsyncer repair {storage.instance_name}\n\n"
            "But make sure to have a backup of your data in some form. The "
            "offending hrefs are:\n\n{href_list}\n".format(
                status_name=status_name,
                storage=e.storage,
                href_list="\n".join(map(repr, e.hrefs)),
            )
        )
    except (click.Abort, KeyboardInterrupt, JobFailed):
        pass
    except exceptions.PairNotFound as e:
        cli_logger.error(
            f"Pair {e.pair_name} does not exist. Please check your "
            "configuration file and make sure you've typed the pair name "
            "correctly"
        )
    except exceptions.InvalidResponse as e:
        cli_logger.error(
            "The server returned something vdirsyncer doesn't understand. "
            f"Error message: {e!r}\n"
            "While this is most likely a serverside problem, the vdirsyncer "
            "devs are generally interested in such bugs. Please report it in "
            f"the issue tracker at {BUGTRACKER_HOME}"
        )
    except exceptions.CollectionRequired:
        cli_logger.error(
            "One or more storages don't support `collections = null`. "
            'You probably want to set `collections = ["from a", "from b"]`.'
        )
    except Exception as e:
        tb = sys.exc_info()[2]
        import traceback

        tb = traceback.format_tb(tb)
        if status_name:
            msg = f"Unknown error occurred for {status_name}"
        else:
            msg = "Unknown error occurred"

        msg += f": {e}\nUse `-vdebug` to see the full traceback."

        cli_logger.error(msg)
        cli_logger.debug("".join(tb))


def get_status_name(pair: str, collection: str | None) -> str:
    if collection is None:
        return pair
    return pair + "/" + collection


def get_status_path(
    base_path: str,
    pair: str,
    collection: str | None = None,
    data_type: str | None = None,
) -> str:
    assert data_type is not None
    status_name = get_status_name(pair, collection)
    path = expand_path(os.path.join(base_path, status_name))
    if os.path.isfile(path) and data_type == "items":
        new_path = path + ".items"
        # XXX: Legacy migration
        cli_logger.warning(f"Migrating statuses: Renaming {path} to {new_path}")
        os.rename(path, new_path)

    path += "." + data_type
    return path


def load_status(
    base_path: str,
    pair: str,
    collection: str | None = None,
    data_type: str | None = None,
) -> dict[str, Any]:
    path = get_status_path(base_path, pair, collection, data_type)
    if not os.path.exists(path):
        return {}
    assert_permissions(path, STATUS_PERMISSIONS)

    with open(path) as f:
        try:
            return dict(json.load(f))
        except ValueError:
            pass

    return {}


def prepare_status_path(path: str) -> None:
    dirname = os.path.dirname(path)

    try:
        os.makedirs(dirname, STATUS_DIR_PERMISSIONS)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


@contextlib.contextmanager
def manage_sync_status(base_path: str, pair_name: str, collection_name: str):
    path = get_status_path(base_path, pair_name, collection_name, "items")
    status = None
    legacy_status = None
    try:
        # XXX: Legacy migration
        with open(path, "rb") as f:
            if f.read(1) == b"{":
                f.seek(0)
                legacy_status = dict(json.load(f))
    except (OSError, ValueError):
        pass

    if legacy_status is not None:
        cli_logger.warning("Migrating legacy status to sqlite")
        os.remove(path)
        status = SqliteStatus(path)
        status.load_legacy_status(legacy_status)
    else:
        prepare_status_path(path)
        status = SqliteStatus(path)

    yield status


def save_status(
    base_path: str,
    pair: str,
    data_type: str,
    data: dict[str, Any],
    collection: str | None = None,
) -> None:
    status_name = get_status_name(pair, collection)
    path = expand_path(os.path.join(base_path, status_name)) + "." + data_type
    prepare_status_path(path)

    with atomic_write(path, mode="w", overwrite=True) as f:
        json.dump(data, f)

    os.chmod(path, STATUS_PERMISSIONS)


def storage_class_from_config(config):
    config = dict(config)
    storage_name = config.pop("type")
    try:
        cls = storage_names[storage_name]
    except KeyError:
        raise exceptions.UserError(f"Unknown storage type: {storage_name}")
    return cls, config


async def storage_instance_from_config(
    config,
    create=True,
    *,
    connector: aiohttp.TCPConnector,
):
    """
    :param config: A configuration dictionary to pass as kwargs to the class
        corresponding to config['type']
    """
    from vdirsyncer.storage.dav import DAVStorage
    from vdirsyncer.storage.http import HttpStorage

    cls, new_config = storage_class_from_config(config)

    if issubclass(cls, DAVStorage) or issubclass(cls, HttpStorage):
        assert connector is not None  # FIXME: hack?
        new_config["connector"] = connector

    try:
        return cls(**new_config)
    except exceptions.CollectionNotFound as e:
        if create:
            config = await handle_collection_not_found(
                config, config.get("collection", None), e=str(e)
            )
            return await storage_instance_from_config(
                config,
                create=False,
                connector=connector,
            )
        else:
            raise
    except Exception:
        return handle_storage_init_error(cls, new_config)


def handle_storage_init_error(cls, config):
    e = sys.exc_info()[1]
    if not isinstance(e, TypeError) or "__init__" not in repr(e):
        raise

    all, required = get_storage_init_args(cls)
    given = set(config)
    missing = required - given
    invalid = given - all

    problems = []

    if missing:
        problems.append(
            "{} storage requires the parameters: {}".format(
                cls.storage_name, ", ".join(missing)
            )
        )

    if invalid:
        problems.append(
            "{} storage doesn't take the parameters: {}".format(
                cls.storage_name, ", ".join(invalid)
            )
        )

    if not problems:
        raise e

    raise exceptions.UserError(
        "Failed to initialize {}".format(config["instance_name"]), problems=problems
    )


def assert_permissions(path: str, wanted: int) -> None:
    permissions = os.stat(path).st_mode & 0o777
    if permissions > wanted:
        cli_logger.warning(
            f"Correcting permissions of {path} from {permissions:o} to {wanted:o}"
        )
        os.chmod(path, wanted)


async def handle_collection_not_found(config, collection, e=None):
    storage_name = config.get("instance_name", None)

    cli_logger.warning(
        "{}No collection {} found for storage {}.".format(
            f"{e}\n" if e else "", json.dumps(collection), storage_name
        )
    )

    if click.confirm("Should vdirsyncer attempt to create it?"):
        storage_type = config["type"]
        cls, config = storage_class_from_config(config)
        config["collection"] = collection
        try:
            args = await cls.create_collection(**config)
            args["type"] = storage_type
            return args
        except NotImplementedError as e:
            cli_logger.error(e)

    raise exceptions.UserError(
        f'Unable to find or create collection "{collection}" for '
        f'storage "{storage_name}". Please create the collection '
        "yourself."
    )
