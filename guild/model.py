# Copyright 2017-2018 TensorHub, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import collections
import hashlib
import logging
import os
import sys

import pkg_resources

import guild.plugin

from guild import config
from guild import entry_point_util
from guild import guildfile
from guild import namespace
from guild import resource

log = logging.getLogger("guild")

_models = entry_point_util.EntryPointResources("guild.models", "model")

ModelRef = collections.namedtuple(
    "ModelRef", [
        "dist_type",
        "dist_name",
        "dist_version",
        "model_name"
    ])

class Model(object):

    def __init__(self, ep):
        self.name = ep.name
        self.dist = ep.dist
        self.modeldef = self._init_modeldef()
        self._fullname = None # lazy
        self._reference = None # lazy

    def __repr__(self):
        return "<%s.%s '%s'>" % (
            self.__class__.__module__,
            self.__class__.__name__,
            self.name)

    @property
    def fullname(self):
        if self._fullname is None:
            pkg_name = namespace.apply_namespace(self.dist.project_name)
            self._fullname = "%s/%s" % (pkg_name, self.name)
        return self._fullname

    @property
    def reference(self):
        if self._reference is None:
            self._reference = self._init_reference()
        return self._reference

    def _init_modeldef(self):
        raise NotImplementedError()

    def _init_reference(self):
        raise NotImplementedError()

class GuildfileModel(Model):
    """A model associated with a guildfile.

    These are generated from GuildfileDistribution instances
    (i.e. distributions that are derrived from modefiles).
    """

    def _init_modeldef(self):
        assert isinstance(self.dist, GuildfileDistribution), self.dist
        return self.dist.get_modeldef(self.name)

    def _init_reference(self):
        src = self.dist.guildfile.src
        if src and os.path.isfile(src):
            version = self._guildfile_hash(src)
        else:
            version = "unknown"
        path = self.dist.guildfile.dir
        if path is not None:
            path = os.path.abspath(path)
        return ModelRef("guildfile", path, version, self.name)

    @staticmethod
    def _guildfile_hash(path):
        try:
            path_bytes = open(path, "rb").read()
        except IOError:
            log.warning(
                "unable to read %s to calculate guildfile hash", path)
            return "-"
        else:
            return hashlib.md5(path_bytes).hexdigest()

class PackageModel(Model):
    """A model associated with a package.

    These are generated by Guild packages.
    """

    def _init_modeldef(self):
        modeldef = _find_dist_modeldef(self.name, self.dist)
        if modeldef is None:
            raise ValueError(
                "undefined model '%s' in %s" % (self.name, self.dist))
        return modeldef

    def _init_reference(self):
        pkg_name = namespace.apply_namespace(self.dist.project_name)
        return ModelRef(
            "package",
            pkg_name,
            self.dist.version,
            self.name)

def _find_dist_modeldef(name, dist):
    for modeldef in _ensure_dist_modeldefs(dist):
        if modeldef.name == name:
            return modeldef
    return None

def _ensure_dist_modeldefs(dist):
    if not hasattr(dist, "_modelefs"):
        dist._modeldefs = _load_dist_modeldefs(dist)
    return dist._modeldefs

def _load_dist_modeldefs(dist):
    modeldefs = []
    try:
        record = dist.get_metadata_lines("RECORD")
    except IOError:
        log.warning(
            "distribution %s missing RECORD metadata - unable to find models",
            dist)
    else:
        for line in record:
            path = line.split(",", 1)[0]
            if os.path.basename(path) in guildfile.NAMES:
                fullpath = os.path.join(dist.location, path)
                _try_acc_modeldefs(fullpath, modeldefs)
    return modeldefs

def _try_acc_modeldefs(path, acc):
    try:
        models = guildfile.from_file(path)
    except Exception as e:
        log.error("unable to load models from %s: %s", path, e)
    else:
        for modeldef in models.models.values():
            acc.append(modeldef)

class GuildfileDistribution(pkg_resources.Distribution):

    def __init__(self, guildfile):
        super(GuildfileDistribution, self).__init__(
            guildfile.dir,
            project_name=self._init_project_name(guildfile))
        self.guildfile = guildfile
        self._entry_map = self._init_entry_map()

    def __repr__(self):
        return "<guild.model.GuildfileDistribution '%s'>" % self.guildfile.dir

    def get_entry_map(self, group=None):
        if group is None:
            return self._entry_map
        else:
            return self._entry_map.get(group, {})

    def get_modeldef(self, name):
        for modeldef_name, modeldef in self.guildfile.models.items():
            if modeldef_name == name:
                return modeldef
        raise ValueError(name)

    @staticmethod
    def _init_project_name(guildfile):
        """Returns a project name for a guildfile distribution.

        Guildfile distribution project names are of the format:

            '.guildfile.' + ESCAPED_GUILDFILE_PATH

        ESCAPED_GUILDFILE_PATH is a 'safe' project name (i.e. will not be
        modified in a call to `pkg_resources.safe_name`) that, when
        unescaped using `_unescape_project_name`, is the relative path of
        the directory containing the guildfile. The modefile name itself
        (e.g. 'guild.yml') is not contained in the path.

        Guildfile paths are relative to the current working directory
        (i.e. the value of os.getcwd() at the time they are generated) and
        always start with '.'.
        """
        pkg_path = os.path.relpath(guildfile.dir)
        if pkg_path[0] != ".":
            pkg_path = os.path.join(".", pkg_path)
        safe_path = _escape_project_name(pkg_path)
        return ".guildfile.%s" % safe_path

    def _init_entry_map(self):
        return {
            "guild.models": {
                name: self._model_entry_point(model)
                for name, model in self.guildfile.models.items()
            },
            "guild.resources": {
                res.fullname: self._resource_entry_point(res.fullname)
                for res in self._guildfile_resources()
            }
        }

    def _model_entry_point(self, model):
        return pkg_resources.EntryPoint(
            name=model.name,
            module_name="guild.model",
            attrs=("GuildfileModel",),
            dist=self)

    def _guildfile_resources(self):
        for modeldef in self.guildfile.models.values():
            for res in modeldef.resources:
                yield res

    def _resource_entry_point(self, name):
        return pkg_resources.EntryPoint(
            name=name,
            module_name="guild.model",
            attrs=("GuildfileResource",),
            dist=self)

def _escape_project_name(name):
    """Escapes name for use as a valie pkg_resources project name."""
    return str(base64.b16encode(name.encode("utf-8")).decode("utf-8"))

def _unescape_project_name(escaped_name):
    """Unescapes names escaped with `_escape_project_name`."""
    return str(base64.b16decode(escaped_name).decode("utf-8"))

class GuildfileResource(resource.Resource):

    def _init_resdef(self):
        assert isinstance(self.dist, GuildfileDistribution), self.dist
        model_name, res_name = _split_res_name(self.name)
        modeldef = self.dist.guildfile.models.get(model_name)
        assert modeldef, (self.name, self.dist)
        resdef = modeldef.get_resource(res_name)
        assert resdef, (self.name, self.dist)
        return resdef

def _split_res_name(name):
    parts = name.split(":", 1)
    if len(parts) != 2:
        raise ValueError("invalid resource name: %s" % name)
    return parts

class PackageModelResource(resource.Resource):

    def _init_resdef(self):
        model_name, res_name = _split_res_name(self.name)
        modeldef = _find_dist_modeldef(model_name, self.dist)
        if modeldef is None:
            raise ValueError(
                "undefined model '%s' in %s"
                % (model_name, self.dist))
        resdef = modeldef.get_resource(res_name)
        if resdef is None:
            raise ValueError(
                "undefined resource '%s%s' in %s"
                % (model_name, res_name, self.dist))
        return resdef

class ModelImportError(ImportError):
    pass

class BadGuildfileDistribution(pkg_resources.Distribution):
    """Distribution for a guildfile that can't be read."""

    def __repr__(self):
        return "<guild.model.BadGuildfileDistribution '%s'>" % self.location

    def get_entry_map(self, group=None):
        return {}

class ModelImporter(object):

    undef = object()

    def __init__(self, path):
        if not self._is_guildfile_dir(path):
            raise ModelImportError(path)
        self.path = path
        self._dist = self.undef # lazy

    @staticmethod
    def _is_guildfile_dir(path):
        return (
            guildfile.dir_has_guildfile(path) or
            os.path.abspath(path) == os.path.abspath(config.cwd()))

    @property
    def dist(self):
        if self._dist is self.undef:
            self._dist = self._init_dist()
        return self._dist

    def _init_dist(self):
        if not os.path.isdir(self.path):
            return None
        try:
            mf = guildfile.from_dir(self.path)
        except guildfile.NoModels:
            return self._plugin_model_dist()
        except Exception as e:
            if log.getEffectiveLevel() <= logging.DEBUG:
                log.exception(self.path)
            log.error("error loading guildfile from %s: %s", self.path, e)
            return BadGuildfileDistribution(self.path)
        else:
            return GuildfileDistribution(mf)

    def _plugin_model_dist(self):
        models_data = []
        plugins_used = set()
        for name, plugin in guild.plugin.iter_plugins():
            for data in plugin.find_models(self.path):
                models_data.append(data)
                plugins_used.add(name)
        if models_data:
            mf = guildfile.Guildfile(
                models_data,
                src="<plugin-generated %s>" % ",".join(plugins_used),
                dir=self.path)
            return GuildfileDistribution(mf)
        else:
            return None

    @staticmethod
    def find_module(_fullname, _path=None):
        return None

def _model_finder(importer, path, _only=False):
    assert isinstance(importer, ModelImporter)
    assert importer.path == path, (importer.path, path)
    if importer.dist:
        yield importer.dist

class GuildfileNamespace(namespace.PrefixNamespace):

    prefix = ".guildfile."

    @staticmethod
    def pip_info(_name):
        raise TypeError("guildfiles cannot be installed using pip")

    def package_name(self, project_name):
        pkg = super(GuildfileNamespace, self).package_name(project_name)
        parts = pkg.split("/", 1)
        decoded_project_name = _unescape_project_name(parts[0])
        rest = "/" + parts[1] if len(parts) == 2 else ""
        return decoded_project_name + rest

def get_path():
    return _models.path()

def set_path(path, clear_cache=False):
    _models.set_path(path, clear_cache)

def insert_path(item):
    path = _models.path()
    try:
        path.remove(item)
    except ValueError:
        pass
    path.insert(0, item)
    _models.set_path(path)

def iter_models():
    for _name, model in _models:
        yield model

def for_name(name):
    return _models.for_name(name)

def iter_():
    for _name, model in _models:
        yield model

def _register_model_finder():
    sys.path_hooks.insert(0, ModelImporter)
    pkg_resources.register_finder(ModelImporter, _model_finder)

_register_model_finder()
