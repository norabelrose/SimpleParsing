from __future__ import annotations

import json
import pickle
from collections import OrderedDict
from dataclasses import MISSING, Field, dataclass, fields, is_dataclass
from functools import partial
from itertools import chain
from logging import getLogger
from pathlib import Path
from typing import IO, Any, Callable, ClassVar, TypeVar, Union

from simple_parsing.utils import get_args, get_forward_arg, is_optional

from .decoding import decode_field, register_decoding_fn
from .encoding import SimpleJsonEncoder, encode

DumpFn = Callable[[Any, IO], None]
DumpsFn = Callable[[Any], str]
LoadFn = Callable[[IO], dict]
LoadsFn = Callable[[str], dict]

logger = getLogger(__name__)

Dataclass = TypeVar("Dataclass")
D = TypeVar("D", bound="SerializableMixin")

try:
    import yaml

    def ordered_dict_constructor(loader: yaml.Loader, node: yaml.Node):
        # NOTE(ycho): `deep` has to be true for `construct_yaml_seq`.
        value = loader.construct_sequence(node, deep=True)
        return OrderedDict(*value)

    def ordered_dict_representer(dumper: yaml.Dumper, instance: OrderedDict) -> yaml.Node:
        # NOTE(ycho): nested list for compatibility with PyYAML's representer
        node = dumper.represent_sequence("OrderedDict", [list(instance.items())])
        return node

    yaml.add_representer(OrderedDict, ordered_dict_representer)
    yaml.add_constructor("OrderedDict", ordered_dict_constructor)
    yaml.add_constructor(
        "tag:yaml.org,2002:python/object/apply:collections.OrderedDict",
        ordered_dict_constructor,
    )

except ImportError:
    pass


class SerializableMixin:
    """Makes a dataclass serializable to and from dictionaries.

    Supports JSON and YAML files for now.

    >>> from dataclasses import dataclass
    >>> from simple_parsing.helpers import Serializable
    >>> @dataclass
    ... class Config(Serializable):
    ...   a: int = 123
    ...   b: str = "456"
    ...
    >>> config = Config()
    >>> config
    Config(a=123, b='456')
    >>> config.to_dict()
    {'a': 123, 'b': '456'}
    >>> config_ = Config.from_dict({"a": 123, "b": 456})
    >>> config_
    Config(a=123, b='456')
    >>> assert config == config_
    """

    subclasses: ClassVar[list[type[D]]] = []
    decode_into_subclasses: ClassVar[bool] = False

    def __init_subclass__(
        cls, decode_into_subclasses: bool | None = None, add_variants: bool = True
    ):
        logger.debug(f"Registering a new Serializable subclass: {cls}")
        super().__init_subclass__()
        if decode_into_subclasses is None:
            # if decode_into_subclasses is None, we will use the value of the
            # parent class, if it is also a subclass of Serializable.
            # Skip the class itself as well as object.
            parents = cls.mro()[1:-1]
            logger.debug(f"parents: {parents}")

            for parent in parents:
                if parent in SerializableMixin.subclasses and parent is not SerializableMixin:
                    decode_into_subclasses = parent.decode_into_subclasses
                    logger.debug(
                        f"Parent class {parent} has decode_into_subclasses = {decode_into_subclasses}"
                    )
                    break

        cls.decode_into_subclasses = decode_into_subclasses or False
        if cls not in SerializableMixin.subclasses:
            SerializableMixin.subclasses.append(cls)

        encode.register(cls, cls.to_dict)
        register_decoding_fn(cls, cls.from_dict)

    def to_dict(self, dict_factory: type[dict] = dict, recurse: bool = True) -> dict:
        """Serializes this dataclass to a dict.

        NOTE: This 'extends' the `asdict()` function from
        the `dataclasses` package, allowing us to not include some fields in the
        dict, or to perform some kind of custom encoding (for instance,
        detaching `Tensor` objects before serializing the dataclass to a dict).
        """
        return to_dict(self, dict_factory=dict_factory, recurse=recurse)

    @classmethod
    def from_dict(cls: type[D], obj: dict, drop_extra_fields: bool | None = None) -> D:
        """Parses an instance of `cls` from the given dict.

        NOTE: If the `decode_into_subclasses` class attribute is set to True (or
        if `decode_into_subclasses=True` was passed in the class definition),
        then if there are keys in the dict that aren't fields of the dataclass,
        this will decode the dict into an instance the first subclass of `cls`
        which has all required field names present in the dictionary.

        Passing `drop_extra_fields=None` (default) will use the class attribute
        described above.
        Passing `drop_extra_fields=True` will decode the dict into an instance
        of `cls` and drop the extra keys in the dict.
        Passing `drop_extra_fields=False` forces the above-mentioned behaviour.
        """
        return from_dict(cls, obj, drop_extra_fields=drop_extra_fields)

    def dump(self, fp: IO[str], dump_fn: DumpFn = json.dump) -> None:
        dump(self, fp=fp, dump_fn=dump_fn)

    def dump_json(self, fp: IO[str], dump_fn: DumpFn = json.dump, **kwargs) -> None:
        return dump_json(self, fp, dump_fn=dump_fn, **kwargs)

    def dump_yaml(self, fp: IO[str], dump_fn: DumpFn | None = None, **kwargs) -> None:
        return dump_yaml(self, fp, dump_fn=dump_fn, **kwargs)

    def dumps(self, dump_fn: DumpsFn = json.dumps, **kwargs) -> str:
        return dumps(self, dump_fn=dump_fn, **kwargs)

    def dumps_json(self, dump_fn: DumpsFn = json.dumps, **kwargs) -> str:
        return dumps_json(self, dump_fn=dump_fn, **kwargs)

    def dumps_yaml(self, dump_fn: DumpsFn | None = None, **kwargs) -> str:
        return dumps_yaml(self, dump_fn=dump_fn, **kwargs)

    @classmethod
    def load(
        cls: type[D],
        path: Path | str | IO[str],
        drop_extra_fields: bool | None = None,
        load_fn: LoadFn | None = None,
        **kwargs,
    ) -> D:
        """Loads an instance of `cls` from the given file.

        Args:
            cls (Type[D]): A dataclass type to load.
            path (Union[Path, str, IO[str]]): Path or Path string or open file.
            drop_extra_fields (bool, optional): Whether to drop extra fields or
                to decode the dictionary into the first subclass with matching
                fields. Defaults to None, in which case we use the value of
                `cls.decode_into_subclasses`.
                For more info, see `cls.from_dict`.
            load_fn (Callable, optional): Which loading function to use. Defaults
                to None, in which case we try to use the appropriate loading
                function depending on `path.suffix`:
                {
                    ".yml": yaml.safe_load,
                    ".yaml": yaml.safe_load,
                    ".json": json.load,
                    ".pth": torch.load,
                    ".pkl": pickle.load,
                }

        Raises:
            RuntimeError: If the extension of `path` is unsupported.

        Returns:
            D: An instance of `cls`.
        """
        return load(cls, path=path, drop_extra_fields=drop_extra_fields, load_fn=load_fn, **kwargs)

    @classmethod
    def _load(
        cls: type[D],
        fp: IO[str],
        drop_extra_fields: bool | None = None,
        load_fn: LoadFn = json.load,
        **kwargs,
    ) -> D:
        return load(cls, path=fp, drop_extra_fields=drop_extra_fields, load_fn=load_fn, **kwargs)

    @classmethod
    def load_json(
        cls: type[D],
        path: str | Path,
        drop_extra_fields: bool | None = None,
        load_fn: LoadFn = json.load,
        **kwargs,
    ) -> D:
        """Loads an instance from the corresponding json-formatted file.

        Args:
            cls (Type[D]): A dataclass type to load.
            path (Union[str, Path]): Path to a json-formatted file.
            load_fn ([type], optional): Loading function to use. Defaults to json.load.

        Returns:
            D: an instance of the dataclass.
        """
        return load_json(cls, path, drop_extra_fields=drop_extra_fields, load_fn=load_fn, **kwargs)

    @classmethod
    def load_yaml(
        cls: type[D],
        path: str | Path,
        drop_extra_fields: bool | None = None,
        load_fn=None,
        **kwargs,
    ) -> D:
        """Loads an instance from the corresponding yaml-formatted file.

        Args:
            cls (Type[D]): A dataclass type to load.
            path (Union[str, Path]): Path to a yaml-formatted file.
            load_fn ([type], optional): Loading function to use. Defaults to
                None, in which case `yaml.safe_load` is used.

        Returns:
            D: an instance of the dataclass.
        """
        return load_yaml(cls, path, load_fn=load_fn, drop_extra_fields=drop_extra_fields, **kwargs)

    def save(self, path: str | Path, dump_fn=None) -> None:
        save(self, path=path, dump_fn=dump_fn)

    def _save(self, path: str | Path, dump_fn: DumpFn = json.dump, **kwargs) -> None:
        save(self, path=path, dump_fn=partial(dump_fn, **kwargs))

    def save_yaml(self, path: str | Path, dump_fn: DumpFn | None = None, **kwargs) -> None:
        save_yaml(self, path, dump_fn=dump_fn, **kwargs)

    def save_json(self, path: str | Path, dump_fn=json.dump, **kwargs) -> None:
        save_json(self, path, dump_fn=dump_fn, **kwargs)

    @classmethod
    def loads(
        cls: type[D],
        s: str,
        drop_extra_fields: bool | None = None,
        load_fn: LoadsFn = json.loads,
    ) -> D:
        return loads(cls, s, drop_extra_fields=drop_extra_fields, load_fn=load_fn)

    @classmethod
    def loads_json(
        cls: type[D],
        s: str,
        drop_extra_fields: bool | None = None,
        load_fn=json.loads,
        **kwargs,
    ) -> D:
        return loads_json(
            cls, s, drop_extra_fields=drop_extra_fields, load_fn=partial(load_fn, **kwargs)
        )

    @classmethod
    def loads_yaml(
        cls: type[D],
        s: str,
        drop_extra_fields: bool | None = None,
        load_fn: LoadsFn | None = None,
        **kwargs,
    ) -> D:
        return loads_yaml(cls, s, drop_extra_fields=drop_extra_fields, load_fn=load_fn, **kwargs)


@dataclass
class Serializable(SerializableMixin):
    """Makes a dataclass serializable to and from dictionaries.

    Supports JSON and YAML files for now.

    >>> from dataclasses import dataclass
    >>> from simple_parsing.helpers import Serializable
    >>> @dataclass
    ... class Config(Serializable):
    ...   a: int = 123
    ...   b: str = "456"
    ...
    >>> config = Config()
    >>> config
    Config(a=123, b='456')
    >>> config.to_dict()
    {'a': 123, 'b': '456'}
    >>> config_ = Config.from_dict({"a": 123, "b": 456})
    >>> config_
    Config(a=123, b='456')
    >>> assert config == config_
    """


@dataclass(frozen=True)
class FrozenSerializable(SerializableMixin):
    """Makes a (frozen) dataclass serializable to and from dictionaries.

    Supports JSON and YAML files for now.

    >>> from dataclasses import dataclass
    >>> from simple_parsing.helpers import Serializable
    >>> @dataclass
    ... class Config(Serializable):
    ...   a: int = 123
    ...   b: str = "456"
    ...
    >>> config = Config()
    >>> config
    Config(a=123, b='456')
    >>> config.to_dict()
    {'a': 123, 'b': '456'}
    >>> config_ = Config.from_dict({"a": 123, "b": 456})
    >>> config_
    Config(a=123, b='456')
    >>> assert config == config_
    """


@dataclass
class SimpleSerializable(SerializableMixin, decode_into_subclasses=True):
    pass


S = TypeVar("S", bound=SerializableMixin)


def get_serializable_dataclass_types_from_forward_ref(
    forward_ref: type, serializable_base_class: type[S] = SerializableMixin
) -> list[type[S]]:
    """Gets all the subclasses of `serializable_base_class` that have the same name as the argument of this forward reference annotation."""
    arg = get_forward_arg(forward_ref)
    potential_classes: list[type] = []
    for serializable_class in serializable_base_class.subclasses:
        if serializable_class.__name__ == arg:
            potential_classes.append(serializable_class)
    return potential_classes


T = TypeVar("T")


def load(
    cls: type[Dataclass],
    path: Path | str | IO,
    drop_extra_fields: bool | None = None,
    load_fn: LoadFn | None = None,
) -> Dataclass:
    """Loads an instance of `cls` from the given file.

    First, `load_fn` is used to get a potentially nested dictionary of python primitives from a
    file. Then, a decoding function is applied to each value, based on the type annotation of the
    corresponding field. Finally, the resulting dictionary is used to instantiate an instance of
    the dataclass `cls`.

    - string -> `load_fn` (json/yaml/etc) -> dict with "raw" python values -> decode -> \
        dict with constructor arguments -> `cls`(**dict) -> instance of `cls`

    NOTE: This does not save the types of the dataclass fields. This is usually not an issue, since
    we can recover the right type to use by looking at subclasses of the annotated type. However,
    in some cases (e.g. subgroups), it might be useful to save all the types of all the
    fields, in which case you should probably use something like `yaml.dump`, directly passing it
    the dataclass, instead of this.

    Args:
        cls (Type[D]): A dataclass type to load.
        path (Path | str): Path or Path string or open file.
        drop_extra_fields (bool, optional): Whether to drop extra fields or
            to decode the dictionary into the first subclass with matching
            fields. Defaults to None, in which case we use the value of
            `cls.decode_into_subclasses`.
            For more info, see `cls.from_dict`.
        load_fn ([type], optional): Which loading function to use. Defaults
            to None, in which case we try to use the appropriate loading
            function depending on `path.suffix`:
            {
                ".yml": yaml.safe_load,
                ".yaml": yaml.safe_load,
                ".json": json.load,
                ".pth": torch.load,
                ".pkl": pickle.load,
            }

    Raises:
        RuntimeError: If the extension of `path` is unsupported.

    Returns:
        D: An instance of `cls`.
    """
    if isinstance(path, str):
        path = Path(path)
    if load_fn is None and isinstance(path, Path):
        # Load a dict from the file.
        d = read_file(path)
    elif load_fn:
        with (path.open() if isinstance(path, Path) else path) as f:
            d = load_fn(f)
    else:
        raise ValueError(
            "A loading function must be passed, since we got an io stream, and the "
            "extension can't be retrieved."
        )
    # Convert the dict into an instance of the class.
    if drop_extra_fields is None and getattr(cls, "decode_into_subclasses", None) is not None:
        drop_extra_fields = not getattr(cls, "decode_into_subclasses")
    return from_dict(cls, d, drop_extra_fields=drop_extra_fields)


def load_json(
    cls: type[Dataclass],
    path: str | Path,
    drop_extra_fields: bool | None = None,
    load_fn: LoadFn = json.load,
    **kwargs,
) -> Dataclass:
    """Loads an instance from the corresponding json-formatted file.

    Args:
        cls (Type[D]): A dataclass type to load.
        path (Union[str, Path]): Path to a json-formatted file.
        load_fn ([type], optional): Loading function to use. Defaults to json.load.

    Returns:
        D: an instance of the dataclass.
    """
    return load(cls, path, drop_extra_fields=drop_extra_fields, load_fn=partial(load_fn, **kwargs))


def loads(
    cls: type[Dataclass],
    s: str,
    drop_extra_fields: bool | None = None,
    load_fn: LoadsFn = json.loads,
) -> Dataclass:
    d = load_fn(s)
    return from_dict(cls, d, drop_extra_fields=drop_extra_fields)


def loads_json(
    cls: type[Dataclass],
    s: str,
    drop_extra_fields: bool | None = None,
    load_fn: LoadsFn = json.loads,
    **kwargs,
) -> Dataclass:
    return loads(cls, s, drop_extra_fields=drop_extra_fields, load_fn=partial(load_fn, **kwargs))


def loads_yaml(
    cls: type[Dataclass],
    s: str,
    drop_extra_fields: bool | None = None,
    load_fn: LoadsFn | None = None,
    **kwargs,
) -> Dataclass:
    import yaml

    load_fn = load_fn or yaml.safe_load
    return loads(cls, s, drop_extra_fields=drop_extra_fields, load_fn=partial(load_fn, **kwargs))


extensions_to_loading_fn: dict[str, Callable[[IO], Any]] = {
    ".json": json.load,
    ".pkl": pickle.load,
}
extensions_to_read_mode: dict[str, str] = {".pkl": "rb"}

extensions_to_write_mode: dict[str, str] = {".pkl": "wb"}
extensions_to_dump_fn: dict[str, Callable[[Any, IO], None]] = {
    ".json": json.dump,
    ".pkl": pickle.dump,
}
try:
    import yaml

    extensions_to_loading_fn[".yaml"] = yaml.safe_load
    extensions_to_loading_fn[".yml"] = yaml.safe_load
    extensions_to_dump_fn[".yaml"] = yaml.dump
    extensions_to_dump_fn[".yml"] = yaml.dump


except ImportError:
    pass

try:
    import numpy  # type: ignore

    extensions_to_loading_fn[".npy"] = numpy.load
    extensions_to_dump_fn[".npy"] = numpy.save
    extensions_to_read_mode[".npy"] = "rb"
    extensions_to_write_mode[".npy"] = "wb"

except ImportError:
    pass

try:
    import torch  # type: ignore

    extensions_to_loading_fn[".pth"] = torch.load
    extensions_to_dump_fn[".pth"] = torch.save
    extensions_to_read_mode[".pth"] = "rb"
    extensions_to_write_mode[".pth"] = "wb"
except ImportError:
    pass


def read_file(path: str | Path) -> dict:
    """Returns the contents of the given file as a dictionary.
    Uses the right function depending on `path.suffix`:
    {
        ".yml": yaml.safe_load,
        ".yaml": yaml.safe_load,
        ".json": json.load,
        ".pth": torch.load,
        ".pkl": pickle.load,
    }
    """
    path = Path(path)
    if path.suffix in extensions_to_loading_fn:
        load_fn = extensions_to_loading_fn[path.suffix]
    else:
        raise RuntimeError(
            f"Unable to determine what function to use in order to load "
            f"path {path} into a dictionary since the path's extension isn't registered in the "
            f"`extensions_to_loading_fn` dictionary..."
        )
    mode = extensions_to_read_mode.get(path.suffix, "r")
    with open(path, mode=mode) as f:
        return load_fn(f)


def save(obj: Any, path: str | Path, dump_fn: Callable[[dict, IO], None] | None = None) -> None:
    """Save the given dataclass or dictionary to the given file.

    Note: The `encode` function is applied to all the object fields to get serializable values,
    like so:
    - obj -> encode -> "raw" values (dicts, strings, ints, etc) -> `dump_fn` ([json/yaml/etc].dumps) -> string
    """
    path = Path(path)

    if not isinstance(obj, dict):
        obj = to_dict(obj)

    if dump_fn:
        save_fn = dump_fn
    elif path.suffix in extensions_to_dump_fn:
        save_fn = extensions_to_dump_fn[path.suffix]
    else:
        raise RuntimeError(
            f"Unable to determine what function to use in order to save obj {obj} to path {path},"
            f"since the path's extension isn't registered in the "
            f"`extensions_to_dump_fn` dictionary..."
        )
    mode = extensions_to_write_mode.get(path.suffix, "w")
    with open(path, mode=mode) as f:
        return save_fn(obj, f)


def save_yaml(obj, path: str | Path, dump_fn: DumpFn | None = None, **kwargs) -> None:
    import yaml

    if dump_fn is None:
        dump_fn = yaml.dump
    save(obj, path, dump_fn=partial(dump_fn, **kwargs))


def save_json(obj, path: str | Path, dump_fn: DumpFn = json.dump, **kwargs) -> None:
    save(obj, path, dump_fn=partial(dump_fn, **kwargs))


def load_yaml(
    cls: type[T],
    path: str | Path,
    drop_extra_fields: bool | None = None,
    load_fn: LoadFn | None = None,
    **kwargs,
) -> T:
    """Loads an instance from the corresponding yaml-formatted file.

    Args:
        cls (Type[T]): A dataclass type to load.
        path (Union[str, Path]): Path to a yaml-formatted file.
        load_fn ([type], optional): Loading function to use. Defaults to
            None, in which case `yaml.safe_load` is used.

    Returns:
        T: an instance of the dataclass.
    """
    import yaml

    if load_fn is None:
        load_fn = yaml.safe_load
    return load(cls, path, drop_extra_fields=drop_extra_fields, load_fn=partial(load_fn, **kwargs))


def dump(dc, fp: IO[str], dump_fn: DumpFn = json.dump) -> None:
    # Convert `dc` into a dict if needed.
    if not isinstance(dc, dict):
        dc = to_dict(dc)
    # Serialize that dict to the file using dump_fn.
    dump_fn(dc, fp)


def dump_json(dc, fp: IO[str], dump_fn: DumpFn = json.dump, **kwargs) -> None:
    return dump(dc, fp, dump_fn=partial(dump_fn, **kwargs))


def dump_yaml(dc, fp: IO[str], dump_fn: DumpFn | None = None, **kwargs) -> None:
    import yaml

    if dump_fn is None:
        dump_fn = yaml.dump
    return dump(dc, fp, dump_fn=partial(dump_fn, **kwargs))


def dumps(dc, dump_fn: DumpsFn = json.dumps) -> str:
    if not isinstance(dc, dict):
        dc = to_dict(dc)
    return dump_fn(dc)


def dumps_json(dc, dump_fn: DumpsFn = json.dumps, **kwargs) -> str:
    kwargs.setdefault("cls", SimpleJsonEncoder)
    return dumps(dc, dump_fn=partial(dump_fn, **kwargs))


def dumps_yaml(dc, dump_fn: DumpsFn | None = None, **kwargs) -> str:
    import yaml

    if dump_fn is None:
        dump_fn = yaml.dump
    return dumps(dc, dump_fn=partial(dump_fn, **kwargs))


def to_dict(dc, dict_factory: type[dict] = dict, recurse: bool = True) -> dict:
    """Serializes this dataclass to a dict.

    NOTE: This 'extends' the `asdict()` function from
    the `dataclasses` package, allowing us to not include some fields in the
    dict, or to perform some kind of custom encoding (for instance,
    detaching `Tensor` objects before serializing the dataclass to a dict).
    """
    if not is_dataclass(dc):
        raise ValueError("to_dict should only be called on a dataclass instance.")

    d: dict[str, Any] = dict_factory()
    for f in fields(dc):
        name = f.name
        value = getattr(dc, name)

        # Do not include in dict if some corresponding flag was set in metadata.
        include_in_dict = f.metadata.get("to_dict", True)
        if not include_in_dict:
            continue

        custom_encoding_fn = f.metadata.get("encoding_fn")
        if custom_encoding_fn:
            # Use a custom encoding function if there is one.
            d[name] = custom_encoding_fn(value)
            continue

        encoding_fn = encode
        # TODO: Make a variant of the serialization tests that use the static functions everywhere.
        if is_dataclass(value) and recurse:
            try:
                encoded = to_dict(value, dict_factory=dict_factory, recurse=recurse)
            except TypeError:
                encoded = to_dict(value)
            logger.debug(f"Encoded dataclass field {name}: {encoded}")
        else:
            try:
                encoded = encoding_fn(value)
            except Exception as e:
                logger.error(
                    f"Unable to encode value {value} of type {type(value)}! Leaving it as-is. (exception: {e})"
                )
                encoded = value
        d[name] = encoded
    return d


def from_dict(
    cls: type[Dataclass], d: dict[str, Any], drop_extra_fields: bool | None = None
) -> Dataclass:
    """Parses an instance of the dataclass `cls` from the dict `d`.

    Args:
        cls (Type[Dataclass]): A `dataclass` type.
        d (Dict[str, Any]): A dictionary of `raw` values, obtained for example
            when deserializing a json file into an instance of class `cls`.
        drop_extra_fields (bool, optional): Whether or not to drop extra
            dictionary keys (dataclass fields) when encountered. There are three
            options:
            - True:
                The extra keys are dropped, and this function returns an
                instance of `cls`.
            - False:
                The extra keys (if any) are kept, and we search through the
                subclasses of `cls` for the first dataclass which has all the
                required fields.
            - None (default):
                `drop_extra_fields = not cls.decode_into_subclasses`.

    Raises:
        RuntimeError: If an error is encountered while instantiating the class.

    Returns:
        Dataclass: An instance of the dataclass `cls`.
    """
    if d is None:
        return None

    obj_dict: dict[str, Any] = d.copy()

    init_args: dict[str, Any] = {}
    non_init_args: dict[str, Any] = {}

    if drop_extra_fields is None:
        drop_extra_fields = not getattr(cls, "decode_into_subclasses", False)
        logger.debug("drop_extra_fields is None. Using cls attribute.")

        if cls in {Serializable, FrozenSerializable, SerializableMixin}:
            # Passing `Serializable` means that we want to find the right
            # subclass depending on the keys.
            # We set the value to False when `Serializable` is passed, since
            # we use this mechanism when we don't know which dataclass to use.
            logger.debug("cls is `SerializableMixin`, drop_extra_fields = False.")
            drop_extra_fields = False

    logger.debug(f"from_dict for {cls}, drop extra fields: {drop_extra_fields}")
    for field in fields(cls) if is_dataclass(cls) else []:
        name = field.name
        if name not in obj_dict:
            if (
                field.metadata.get("to_dict", True)
                and field.default is MISSING
                and field.default_factory is MISSING
            ):
                logger.warning(
                    f"Couldn't find the field '{name}' in the dict with keys " f"{list(d.keys())}"
                )
            continue

        raw_value = obj_dict.pop(name)
        field_value = decode_field(field, raw_value, containing_dataclass=cls)

        if field.init:
            init_args[name] = field_value
        else:
            non_init_args[name] = field_value

    extra_args = obj_dict

    # If there are arguments left over in the dict after taking all fields.
    if extra_args:
        if drop_extra_fields:
            logger.warning(f"Dropping extra args {extra_args}")
            extra_args.clear()

        elif issubclass(cls, (Serializable, FrozenSerializable, SerializableMixin)):
            # Use the first Serializable derived class that has all the required
            # fields.
            logger.debug(f"Missing field names: {extra_args.keys()}")

            # Find all the "registered" subclasses of `cls`. (from Serializable)
            derived_classes: list[type[SerializableMixin]] = []
            for subclass in cls.subclasses:
                if issubclass(subclass, cls) and subclass is not cls:
                    derived_classes.append(subclass)
            logger.debug(f"All serializable derived classes of {cls} available: {derived_classes}")

            # All the arguments that the dataclass should be able to accept in
            # its 'init'.
            req_init_field_names = set(chain(extra_args, init_args))

            # Sort the derived classes by their number of init fields, so that
            # we choose the first one with all the required fields.
            derived_classes.sort(key=lambda dc: len(get_init_fields(dc)))

            for child_class in derived_classes:
                logger.debug(f"child class: {child_class.__name__}, mro: {child_class.mro()}")
                child_init_fields: dict[str, Field] = get_init_fields(child_class)
                child_init_field_names = set(child_init_fields.keys())

                if child_init_field_names >= req_init_field_names:
                    # `child_class` is the first class with all required fields.
                    logger.debug(f"Using class {child_class} instead of {cls}")
                    return from_dict(child_class, d, drop_extra_fields=False)

    init_args.update(extra_args)
    try:
        instance = cls(**init_args)  # type: ignore
    except TypeError as e:
        # raise RuntimeError(f"Couldn't instantiate class {cls} using init args {init_args}.")
        raise RuntimeError(
            f"Couldn't instantiate class {cls} using init args {init_args.keys()}: {e}"
        )

    for name, value in non_init_args.items():
        logger.debug(f"Setting non-init field '{name}' on the instance.")
        setattr(instance, name, value)
    return instance


def get_init_fields(dataclass: type) -> dict[str, Field]:
    result: dict[str, Field] = {}
    for field in fields(dataclass):
        if field.init:
            result[field.name] = field
    return result


def get_first_non_None_type(optional_type: type | tuple[type, ...]) -> type | None:
    if not isinstance(optional_type, tuple):
        optional_type = get_args(optional_type)
    for arg in optional_type:
        if arg is not Union and arg is not type(None):  # noqa: E721
            logger.debug(f"arg: {arg} is not union? {arg is not Union}")
            logger.debug(f"arg is not type(None)? {arg is not type(None)}")
            return arg
    return None


def is_dataclass_or_optional_dataclass_type(t: type) -> bool:
    """Returns whether `t` is a dataclass type or an Optional[<dataclass type>]."""
    return is_dataclass(t) or (is_optional(t) and is_dataclass(get_args(t)[0]))
