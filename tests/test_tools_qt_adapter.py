"""QtSlicerAdapter против минимальных заглушек API UM / steslicer.

Заглушки повторяют только те вызовы, которые делает адаптер (сигнатуры сверены с
исходниками STE Slicer). Работу в настоящем слайсере это не доказывает — она проверялась
отдельно, вручную, — но каждая ветка кода выполняется, так что ошибки уровня
NameError/AttributeError и путаница в системах координат ловятся здесь.

Запуск из корня репозитория: python -m unittest tests.test_tools_qt_adapter
"""

import importlib
import os
import shutil
import sys
import tempfile
import threading
import types
import unittest
import zipfile

PLUGIN_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugin", "AIAssistant")
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)

from agent import main_thread  # noqa: E402
from agent.slicer import coords  # noqa: E402

numpy = coords.numpy


# Заглушки API слайсера

class Vector(object):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class Quaternion(object):
    def __init__(self, x=0.0, y=0.0, z=0.0, w=1.0):
        self.x, self.y, self.z, self.w = x, y, z, w


class Signal(object):
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def disconnect(self, slot):
        self.slots.remove(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


class AABB(object):
    def __init__(self, lo, hi):
        self.left, self.bottom, self.back = lo
        self.right, self.top, self.front = hi
        self.width, self.height, self.depth = hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]
        self.center = Vector((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


class Matrix(object):
    """UM.Math.Matrix: 4x4, векторы-столбцы (только то, чем пользуется адаптер)."""

    def __init__(self, data=None):
        self._data = numpy.identity(4) if data is None else numpy.array(data, dtype=float)

    def getData(self):
        return self._data

    def setByScaleVector(self, scale):
        self._data = numpy.diag([scale.x, scale.y, scale.z, 1.0])

    def multiply(self, other, copy=False):
        data = numpy.dot(self._data, other.getData())
        if copy:
            return Matrix(data)
        self._data = data
        return self


class Mesh(object):
    def __init__(self, vertices, indices):
        self._v, self._i = vertices, indices

    def getTransformed(self, world):
        if isinstance(world, Matrix):   # как transformVertices из UM/Mesh/MeshData.py
            data = world.getData()
            return Mesh(self._v.dot(data[:3, :3].T) + data[:3, 3], self._i)
        rotation, scale, translation = world
        return Mesh((self._v * scale).dot(numpy.asarray(rotation).T) + translation, self._i)

    def getVertices(self):
        return self._v

    def hasIndices(self):
        return self._i is not None

    def getIndices(self):
        return self._i


class SteSlicerSceneNode(object):
    def __init__(self, name, vertices, faces):
        self._name = name
        self._mesh = Mesh(numpy.asarray(vertices, dtype=float), numpy.asarray(faces))
        self.rotation = numpy.eye(3)
        self.scale_v = numpy.ones(3)
        self.position = numpy.zeros(3)
        self.parent = None
        self.outside = False

    # decorations
    def callDecoration(self, name, *args):
        return {"isSliceable": True, "isGroup": False, "getBuildPlateNumber": 0}.get(name)

    def getName(self):
        return self._name

    def getParent(self):
        return self.parent

    def getChildren(self):
        return []

    def getMeshData(self):
        return self._mesh

    def getWorldTransformation(self):
        return (self.rotation, self.scale_v, self.position)

    def getBoundingBox(self):
        verts = self._mesh.getTransformed(self.getWorldTransformation()).getVertices()
        return AABB(verts.min(axis=0).tolist(), verts.max(axis=0).tolist())

    def getOrientation(self):
        x, y, z, w = coords.matrix_to_quaternion(self.rotation.tolist())
        return Quaternion(x, y, z, w)

    def getScale(self):
        return Vector(*self.scale_v.tolist())

    def getPosition(self):
        return Vector(*self.position.tolist())

    def isOutsideBuildArea(self):
        return self.outside

    def setOutsideBuildArea(self, value):
        self.outside = value


class _Op(object):
    def push(self):
        self.redo()


class TranslateOperation(_Op):
    def __init__(self, node, translation, set_position=False):
        self.node, self.t = node, translation

    def redo(self):
        self.node.position = self.node.position + [self.t.x, self.t.y, self.t.z]


class RotateOperation(_Op):
    def __init__(self, node, rotation, rotate_around_point=Vector()):
        self.node, self.q, self.p = node, rotation, rotate_around_point

    def redo(self):  # поворот вокруг начала узла, положение не меняется (см. RotateOperation.redo в UM)
        matrix = numpy.asarray(coords.quaternion_to_matrix((self.q.x, self.q.y, self.q.z, self.q.w)))
        self.node.rotation = matrix.dot(self.node.rotation)


class SetTransformOperation(_Op):
    def __init__(self, node, translation=None, orientation=None, scale=None, shear=None, mirror=None):
        self.node, self.scale = node, scale

    def redo(self):
        self.node.scale_v = numpy.asarray([self.scale.x, self.scale.y, self.scale.z])


class RemoveSceneNodeOperation(_Op):
    def __init__(self, node):
        self.node = node

    def redo(self):
        FakeApp.instance.scene_nodes.remove(self.node)


class GroupedOperation(_Op):
    def __init__(self):
        self.ops = []

    def addOperation(self, op):
        self.ops.append(op)

    def redo(self):
        for op in self.ops:
            op.redo()


class Root(object):
    def callDecoration(self, name, *args):
        return None


def DepthFirstIterator(root):
    return [root] + list(FakeApp.instance.scene_nodes)


class Container(object):
    def __init__(self, cid, ctype, values=None, metadata=None, name=None):
        self._id, self.type, self.values = cid, ctype, dict(values or {})
        self.metadata = dict(metadata or {})
        self.metadata["type"] = ctype
        self._name = name or cid

    def getId(self):
        return self._id

    def getName(self):
        return self._name

    def getMetaDataEntry(self, key, default=None):
        return self.metadata.get(key, default)

    def hasProperty(self, key, prop):
        return key in self.values

    def getAllKeys(self):
        return set(self.values)

    def removeInstance(self, key, postpone_emit=False):
        self.values.pop(key, None)

    def clear(self):
        self.values.clear()

    def findDefinitions(self, key=None):
        if key is None:
            return [types.SimpleNamespace(key=k, label=d["label"], description="", type=d["type"],
                                          unit=d.get("unit", "")) for k, d in DEFINITIONS.items()]
        return [key] if key in DEFINITIONS else []

    def getProperty(self, key, prop):
        return DEFINITIONS.get(key, {}).get("value") if prop == "value" else DEFINITIONS.get(key, {}).get(prop)


DEFINITIONS = {
    "printing_mode": {"type": "enum", "label": "Printing Mode", "value": "cylindrical_full", "per": False,
                      "options": {"classic": "3D", "cylindrical": "5D Spiral", "cylindrical_full": "5D"}},
    "layer_height": {"type": "float", "label": "Layer Height", "unit": "mm", "value": 0.1, "per": False,
                     "minimum_value": 0.001, "maximum_value_warning": 0.32},
    "infill_sparse_density": {"type": "float", "label": "Infill Density", "unit": "%", "value": 20, "per": True,
                              "minimum_value": 0},
    "material_print_temperature": {"type": "float", "label": "Printing Temperature", "value": 200, "per": True,
                                   "maximum_value": 365},
    "material_diameter": {"type": "float", "label": "Diameter", "value": 1.75, "per": True},
    "machine_nozzle_size": {"type": "float", "label": "Nozzle", "value": 0.4, "per": True},
    "machine_extruder_count": {"type": "int", "label": "Extruders", "value": 2, "per": False},
    "machine_width": {"type": "float", "label": "W", "value": 351.0, "per": False},
    "machine_depth": {"type": "float", "label": "D", "value": 300.3, "per": False},
    "machine_height": {"type": "float", "label": "H", "value": 231.1, "per": False},
    "machine_shape": {"type": "enum", "label": "Shape", "value": "elliptic", "per": False},
    "adhesion_type": {"type": "enum", "label": "Adhesion", "value": "raft", "per": False},
    "support_infill_rate": {"type": "float", "label": "Support Density", "value": 15, "per": True,
                            "limit_to_extruder": "1"},
}


class Stack(object):
    def __init__(self, sid, global_stack=None, material=None):
        self._id = sid
        self.userChanges = Container(sid + "_user", "user")
        self.qualityChanges = Container("empty_quality_changes", "quality_changes")
        self.quality = Container("normal", "quality", metadata={"quality_type": "normal"})
        self.material = material or Container("generic_pla_175", "material",
                                              metadata={"base_file": "generic_pla_175", "compatible": True},
                                              name="PLA")
        self.definition = Container("hte530v5.2.4", "definition", name="Stereotech Hybrid 530 V5.2.4")
        self.global_stack = global_stack
        self.extruders = {}
        self.isEnabled = True

    def getId(self):
        return self._id

    def getName(self):
        return self._id

    def getTop(self):
        return self.userChanges

    def getContainers(self):
        return [self.userChanges, self.qualityChanges, self.quality, self.material, self.definition]

    def getNextStack(self):
        return self.global_stack

    def getMetaDataEntry(self, key, default=None):
        return {"properties": {"density": 1.24}}.get(key, default)

    def setProperty(self, key, prop, value):
        self.userChanges.values[key] = value

    def getProperty(self, key, prop):
        definition = DEFINITIONS.get(key)
        if definition is None:
            return None
        if prop == "value":
            for container in self.getContainers()[:-1]:
                if key in container.values:
                    return container.values[key]
            if self.global_stack is not None:
                return self.global_stack.getProperty(key, prop)
            return definition["value"]
        if prop == "settable_per_extruder":
            return definition["per"]
        if prop == "enabled":
            return True
        if prop == "validationState":
            value = self.getProperty(key, "value")
            maximum = definition.get("maximum_value")
            return types.SimpleNamespace(value="MaximumError" if maximum is not None and value > maximum
                                         else "Valid")
        if prop == "limit_to_extruder":
            return definition.get("limit_to_extruder", "-1")
        return definition.get(prop)

    def getErrorKeys(self):
        return [k for k in DEFINITIONS if self.getProperty(k, "validationState").value.endswith("Error")]


class QualityGroup(object):
    def __init__(self, name, quality_type, layer_height):
        self.name, self.quality_type, self.is_available = name, quality_type, True
        values = {"layer_height": layer_height} if layer_height else {}
        self.node_for_global = types.SimpleNamespace(getContainer=lambda: Container(quality_type, "quality", values))


class QualityManager(object):
    def __init__(self, app):
        self.app = app
        self.groups = {"normal": QualityGroup("Fine", "normal", None), "draft": QualityGroup("Draft", "draft", 0.2)}
        self.custom = {}

    def getQualityGroups(self, stack):
        return self.groups

    def getQualityChangesGroups(self, stack):
        return self.custom

    def createQualityChanges(self, name):
        group = QualityGroup(name, "normal", None)
        self.pending = group
        for s in [self.app.stack] + list(self.app.stack.extruders.values()):
            s.userChanges.clear()

    def initialize(self):
        self.custom[self.pending.name] = self.pending


class MachineManager(object):
    def __init__(self, app):
        self.app = app
        self.activeQualityType = "normal"
        self.activeQualityChangesGroup = None
        self.stacksHaveErrors = False

    @property
    def activeQualityOrQualityChangesName(self):
        return self.activeQualityChangesGroup.name if self.activeQualityChangesGroup else "Fine"

    @property
    def numUserSettings(self):
        return sum(len(s.userChanges.values) for s in [self.app.stack] + list(self.app.stack.extruders.values()))

    def setQualityGroup(self, group, no_dialog=False):
        assert no_dialog
        self.activeQualityType = group.quality_type
        self.activeQualityChangesGroup = None

    def setQualityChangesGroup(self, group, no_dialog=False):
        assert no_dialog
        self.activeQualityChangesGroup = group

    def setMaterial(self, position, container_node):
        assert self.app.prefs["steslicer/choice_on_profile_override"] == "always_keep"
        assert isinstance(position, str)
        root_id = container_node.getMetadata()["base_file"]
        ext = self.app.stack.extruders[position]
        ext.material = Container(root_id, "material", metadata={"base_file": root_id}, name=root_id.upper())

    @staticmethod
    def getMachine(definition_id):
        return None

    def correctExtruderSettings(self):
        pass


class MaterialManager(object):
    def getAvailableMaterialsForMachineExtruder(self, stack, extruder):
        def node(root, name, brand, material):
            meta = {"base_file": root, "name": name, "brand": brand, "material": material,
                    "properties": {"density": 1.3, "diameter": 1.75}}
            return types.SimpleNamespace(getMetadata=lambda: meta)
        return {"enduse_petg": node("enduse_petg", "Enduse PETG", "Stereotech", "Enduse"),
                "generic_pla_175": node("generic_pla_175", "PLA", "Generic", "PLA")}


class Backend(object):
    # Как UM.Backend.Backend в настоящем слайсере: для наследников Backend(QObject, ...) @signalemitter
    # не срабатывает, поэтому backendStateChange / processingProgress — ОДИН общий сигнал класса на все
    # бэкенды, а backendError объявлен в каждом классе бэкенда.
    backendStateChange = None
    processingProgress = None

    def __init__(self, plugin_id, app):
        self.plugin_id, self.app = plugin_id, app
        self.backendError = Signal()

    def getPluginId(self):
        return self.plugin_id

    def forceSlice(self):
        self.backendStateChange.emit(1)    # stopSlicing()
        self.backendStateChange.emit(2)

    def stopSlicing(self):
        self.backendStateChange.emit(1)

    def finish(self):
        self.app.gcode = ";FLAVOR:RepRap\n;LAYER:0\nG1 X1 Y1\n;LAYER:1\nG1 X2 Y2\n"
        self.app.scene.gcode_dict = {0: [self.app.gcode]}
        self.processingProgress.emit(1.0)
        self.backendStateChange.emit(3)


class BackendManager(object):
    def __init__(self, app):
        Backend.backendStateChange, Backend.processingProgress = Signal(), Signal()
        self.backends = {"CuraEngineBackend": Backend("CuraEngineBackend", app),
                         "CylindricalBackend": Backend("CylindricalBackend", app)}
        self.current = self.backends["CylindricalBackend"]
        self.currendBackendChanged = Signal()

    def getBackends(self):
        return self.backends

    def getCurrentBackend(self):
        return self.current


class Writer(object):
    """Ведёт себя как писатели с @call_on_qt_thread: из главного потока вызывать нельзя."""

    def __init__(self, app, kind):
        self.app, self.kind = app, kind

    def write(self, stream, nodes, mode):
        assert threading.current_thread() is not self.app.main, "writer called from the main thread (dead-lock)"
        if self.kind == "gcode":
            stream.write(";PRINTING_MODE:[%s]\n" % self.app.stack.getProperty("printing_mode", "value"))
            stream.write(self.app.gcode)
        else:
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("3D/3dmodel.model", "<model/>")
                archive.writestr("SteSlicer/x.global.cfg", "[general]\n")
        return True

    def getInformation(self):
        return ""


class FakeApp(object):
    instance = None

    def __init__(self):
        FakeApp.instance = self
        self.main = threading.current_thread()
        self.stack = Stack("H530")
        for i in (0, 1):
            ext = Stack("ext%d" % i, global_stack=self.stack)
            self.stack.extruders[str(i)] = ext
        self.prefs = {"steslicer/choice_on_profile_override": "always_ask", "general/auto_slice": False,
                      "physics/automatic_drop_down": True}
        self.machine_manager = MachineManager(self)
        self.quality_manager = QualityManager(self)
        self.backend_manager = BackendManager(self)
        self.scene_nodes = []
        self.scene = types.SimpleNamespace(getRoot=lambda: Root(), gcode_dict={})
        self.fileCompleted = Signal()
        self.globalContainerStackChanged = Signal()
        self.gcode = ""
        self.extruder_index = 0

    # API UM.Application, которым пользуется адаптер
    def getMainThread(self):
        return self.main

    def callLater(self, func, *args):
        func(*args)

    def getGlobalContainerStack(self):
        return self.stack

    def getMachineManager(self):
        return self.machine_manager

    def getQualityManager(self):
        return self.quality_manager

    def getMaterialManager(self):
        return MaterialManager()

    def getContainerRegistry(self):
        return types.SimpleNamespace(uniqueName=lambda name: name, findContainerStacks=lambda **kw: [self.stack],
                                     findDefinitionContainersMetadata=lambda **kw: [{"name": "H530"}])

    def getPreferences(self):
        prefs = self.prefs
        return types.SimpleNamespace(getValue=prefs.get, setValue=prefs.__setitem__)

    def getBackendManager(self):
        return self.backend_manager

    def getBackend(self):
        return self.backend_manager.current

    def getController(self):
        return types.SimpleNamespace(getScene=lambda: self.scene)

    def getMultiBuildPlateModel(self):
        return types.SimpleNamespace(activeBuildPlate=0)

    def getBuildVolume(self):
        return types.SimpleNamespace(updateNodeBoundaryCheck=lambda: None)

    def getMachineErrorChecker(self):
        return types.SimpleNamespace(needToWaitForResult=False, hasError=self.machine_manager.stacksHaveErrors)

    def getPrintInformation(self):
        return types.SimpleNamespace(currentPrintTime=3725, _material_amounts=[2405.28, 0.0], materialCosts=[0.0],
                                     materialNames=["PLA"], materialLengths=[1.0], materialWeights=[3.0],
                                     jobName="box", getFeaturePrintTimes=lambda: {"infill": 100})

    def getMeshFileHandler(self):
        return types.SimpleNamespace(getReaderForFile=lambda path: object(),
                                     getWriter=lambda name: Writer(self, "gcode"))

    def getWorkspaceFileHandler(self):
        return types.SimpleNamespace(getWriter=lambda name: Writer(self, "3mf"))

    def arrangeAll(self):
        for i, node in enumerate(self.scene_nodes):
            node.position = node.position + [50.0 * (i + 1), 0.0, 0.0]

    def readLocalFile(self, url, skip_project_file_check=False):
        assert skip_project_file_check
        # файл (x, y, z) -> сцена (x, z, -y), как у читателя STL; сетка центрирована, как в MeshFileHandler
        verts = [(0, 0, 0), (40, 0, 0), (40, 20, 0), (0, 20, 0), (0, 0, 10), (40, 0, 10), (40, 20, 10),
                 (0, 20, 10)]
        verts = [(x - 20, z - 5, -(y - 10)) for x, y, z in verts]
        faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
                 (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
        node = SteSlicerSceneNode(os.path.basename(url.path), verts, faces)
        node.position = numpy.array([70.0, -5.0, 30.0])     # место от расстановщика, на стол ещё не опущен
        self.scene_nodes.append(node)
        self.fileCompleted.emit(url.path)


def install_fakes():
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod
        return mod

    for name in ("UM", "UM.Math", "UM.Mesh", "UM.Operations", "UM.Scene", "UM.Scene.Iterator", "UM.Settings",
                 "steslicer", "steslicer.Scene", "steslicer.Settings", "PyQt5"):
        module(name)
    module("UM.Application", Application=types.SimpleNamespace(getInstance=lambda: FakeApp.instance))
    module("UM.Math.Matrix", Matrix=Matrix)
    module("UM.Math.Quaternion", Quaternion=Quaternion)
    module("UM.Math.Vector", Vector=Vector)
    module("UM.Mesh.MeshWriter", MeshWriter=types.SimpleNamespace(
        OutputMode=types.SimpleNamespace(TextMode=1, BinaryMode=2)))
    module("UM.Operations.GroupedOperation", GroupedOperation=GroupedOperation)
    module("UM.Operations.RemoveSceneNodeOperation", RemoveSceneNodeOperation=RemoveSceneNodeOperation)
    module("UM.Operations.RotateOperation", RotateOperation=RotateOperation)
    module("UM.Operations.TranslateOperation", TranslateOperation=TranslateOperation)
    module("UM.Operations.SetTransformOperation", SetTransformOperation=SetTransformOperation)
    module("UM.Scene.Iterator.DepthFirstIterator", DepthFirstIterator=DepthFirstIterator)
    module("UM.Scene.Selection", Selection=types.SimpleNamespace(clear=lambda: None))
    module("steslicer.Scene.SteSlicerSceneNode", SteSlicerSceneNode=SteSlicerSceneNode)
    extruder_manager = types.SimpleNamespace(activeExtruderIndex=0,
                                             setActiveExtruderIndex=lambda i: None)
    module("steslicer.Settings.ExtruderManager",
           ExtruderManager=types.SimpleNamespace(getInstance=lambda: extruder_manager))
    module("steslicer.Settings.MachineManager", MachineManager=MachineManager)
    module("steslicer.Settings.ContainerManager", ContainerManager=types.SimpleNamespace(
        getInstance=lambda: types.SimpleNamespace(updateQualityChanges=lambda: True)))
    module("PyQt5.QtCore", QUrl=types.SimpleNamespace(fromLocalFile=lambda p: types.SimpleNamespace(path=p)))


def remove_fakes():
    for name in list(sys.modules):
        if name == "UM" or name.startswith(("UM.", "steslicer", "PyQt5")):
            if isinstance(sys.modules[name], types.ModuleType) and not getattr(sys.modules[name], "__file__", None):
                del sys.modules[name]


@unittest.skipIf(numpy is None, "для заглушки сцены нужен numpy")
class QtAdapterOnFakesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import UM  # noqa: F401  (the real Uranium is importable: do not shadow it)
            raise unittest.SkipTest("real UM package present")
        except ImportError:
            pass
        install_fakes()
        from agent.slicer import qt_adapter
        cls.module = importlib.reload(qt_adapter)

    @classmethod
    def tearDownClass(cls):
        remove_fakes()
        from agent.slicer import qt_adapter
        importlib.reload(qt_adapter)

    def setUp(self):
        self.app = FakeApp()
        main_thread.set_app_provider(lambda: self.app)
        self.adapter = self.module.QtSlicerAdapter()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        main_thread.set_app_provider(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def in_worker(self, func, *args):
        box = {}

        def run():
            try:
                box["result"] = func(*args)
            except BaseException as exc:
                box["error"] = exc
        thread = threading.Thread(target=run)
        thread.start()
        thread.join(30)
        if "error" in box:
            raise box["error"]
        return box["result"]

    def test_settings(self):
        info = self.adapter.setting_info("infill_sparse_density")
        self.assertEqual(info["stack"], "extruder_0")
        self.assertEqual(info["source"], "глобальный стек: значение по умолчанию")
        info = self.adapter.set_setting("infill_sparse_density", 40.0)
        self.assertEqual(info["value"], 40.0)
        self.assertEqual(info["source"], "пользовательское изменение")
        self.assertEqual(self.adapter.setting_info("support_infill_rate")["stack"], "extruder_1")  # limit_to_extruder
        self.assertEqual(self.adapter.setting_info("layer_height", scope="extruder")["stack"], "global")
        self.assertIsNone(self.adapter.setting_info("nope"))
        enum = self.adapter.setting_info("printing_mode")
        self.assertIn(["classic", "3D"], enum["options"])
        self.adapter.set_setting("material_print_temperature", 500.0)
        self.app.machine_manager.stacksHaveErrors = True
        errors = self.adapter.setting_errors()
        self.assertEqual([(e["key"], e["stack"]) for e in errors], [("material_print_temperature", "extruder_0")])
        self.assertTrue(self.adapter.slicing_precheck()["errors"])
        self.app.machine_manager.stacksHaveErrors = False
        self.adapter.set_setting("printing_mode", "classic")
        reset = self.adapter.reset_settings()
        self.assertEqual(reset["kept"], {"printing_mode": "classic"})
        self.assertEqual(self.app.stack.userChanges.values, {"printing_mode": "classic"})
        self.assertTrue(self.adapter.search_settings("density"))
        config = self.adapter.config()
        self.assertEqual(config["definition_id"], "hte530v5.2.4")
        self.assertEqual(config["user_changes"]["count"], 1)

    def test_profiles_and_materials(self):
        profiles = self.adapter.list_profiles()
        self.assertEqual([q["quality_type"] for q in profiles["quality_profiles"]], ["normal", "draft"])
        self.assertEqual(profiles["quality_profiles"][0]["layer_height_mm"], 0.1)
        self.assertEqual(self.adapter.select_quality("Draft")["quality_type"], "draft")
        self.app.stack.setProperty("layer_height", "value", 0.25)
        created = self.adapter.create_profile("Мой")
        self.assertTrue(created["activated"])
        self.assertEqual(created["settings_moved_to_profile"], 1)
        materials = self.adapter.list_materials()
        self.assertEqual(materials[0]["id"], "enduse_petg")
        self.assertEqual(materials[0]["type"], "PETG")
        result = self.adapter.set_material("petg")      # неоднозначное имя разрешается по типу, если тип один
        self.assertEqual(result["material_id"], "enduse_petg")
        self.assertEqual(self.app.prefs["steslicer/choice_on_profile_override"], "always_ask")   # restored

    def test_scene_and_slicing(self):
        path = os.path.join(self.tmp, "box.stl")
        open(path, "w").close()
        loaded = self.in_worker(self.adapter.load_model, path, False, 5.0, None, None)
        obj = loaded["loaded"][0]
        self.assertEqual(obj["size_mm"], [40.0, 20.0, 10.0])          # система принтера: X, Y (глубина), Z (высота)
        self.assertTrue(obj["on_bed"] and obj["centered_on_5d_axis"])  # 5D: единственный объект ставится на ось
        self.assertAlmostEqual(obj["volume_cm3"], 8.0, places=3)
        self.assertAlmostEqual(obj["bed_contact_area_mm2"], 800.0, places=1)
        moved = self.adapter.move_object("1", x=10, y=20)
        self.assertEqual(moved["center_mm"][:2], [10.0, 20.0])
        # поворот на 90° вокруг X принтера = X сцены: брусок встаёт
        matrix = coords.axis_angle_matrix((1, 0, 0), 90)
        rotated = self.adapter.apply_rotation_matrix("1", matrix, frame="printer")
        self.assertEqual(rotated["size_mm"], [40.0, 10.0, 20.0])
        self.assertEqual(rotated["center_mm"][:2], [10.0, 20.0])
        self.assertEqual(rotated["rotation"]["axis"], [1.0, 0.0, 0.0])
        reset = self.adapter.apply_rotation_matrix("1", coords.axis_angle_matrix((0, 1, 0), 90), reset=True)
        self.assertEqual(reset["size_mm"], [20.0, 40.0, 10.0])       # остаётся только поворот вокруг Y сцены (вертикали)
        flat = self.adapter.lay_flat("1")
        self.assertEqual(flat["size_mm"][2], 10.0)
        scaled = self.adapter.scale_object("1", (1.0, 1.0, 2.0))
        self.assertEqual(scaled["size_mm"][2], 20.0)
        centered = self.adapter.center_for_5d("1")
        self.assertTrue(centered["centered_on_5d_axis"])
        verts = self.adapter.get_object_world_vertices("1", frame="printer")
        self.assertAlmostEqual(min(v[2] for v in verts), 0.0, places=6)
        points, faces = self.adapter.get_object_mesh("1")
        self.assertEqual(len(faces), 12)
        # Собственная система: масштаб (Z принтера = Y сцены, вдвое) без поворотов и положения.
        local, local_faces = self.adapter.get_object_local_mesh("1")
        self.assertEqual(local_faces, faces)
        size = [max(v[i] for v in local) - min(v[i] for v in local) for i in range(3)]
        self.assertEqual([round(x, 6) for x in size], [40.0, 20.0, 20.0])   # scene X, Y (up), Z
        arranged = self.in_worker(self.adapter.arrange_all, 3.0)
        self.assertIn("warning", arranged)

        start = self.adapter.start_slicing()
        self.assertTrue(start["started"])
        self.assertEqual(self.adapter.monitor_snapshot()["state"], "processing")
        self.app.getBackend().finish()
        self.assertEqual(self.adapter.slicing_status()["state"], "done")
        result = self.adapter.slicing_result()
        self.assertEqual(result["print_time_text"], "1 ч 02 мин")
        self.assertEqual(result["material"][0]["length_m"], 1.0)      # 2405.28 mm3 / (pi * 0.875^2) / 1000
        self.assertEqual(result["layers"], 2)
        gcode = self.in_worker(self.adapter.export_gcode, os.path.join(self.tmp, "out.gcode"))
        self.assertEqual(gcode["printing_mode"], "cylindrical_full")
        self.assertEqual(gcode["layers"], 2)
        project = self.in_worker(self.adapter.save_project, os.path.join(self.tmp, "p.3mf"))
        self.assertTrue(project["looks_valid"])
        with self.assertRaises(self.module.SlicerError):   # из главного потока это зависло бы
            self.adapter.export_gcode(os.path.join(self.tmp, "x.gcode"))
        cancelled = self.adapter.cancel_slicing()
        self.assertEqual(cancelled["state"], "not_started")
        deleted = self.adapter.delete_objects(delete_all=True)
        self.assertEqual(deleted["remaining"], 0)

    def test_shared_backend_signals(self):
        # один слушатель на общий сигнал, события относятся к текущему бэкенду
        self.assertEqual(len(Backend.backendStateChange.slots), 1)
        self.assertEqual(len(Backend.processingProgress.slots), 1)
        start = self.adapter.start_slicing()
        self.assertTrue(start["started"])
        self.assertEqual(start["backend"], "CylindricalBackend")
        Backend.processingProgress.emit(0.5)
        Backend.processingProgress.emit(0.0)      # CylindricalBackend перезапускает помощника CuraEngine (_terminate)
        snapshot = self.adapter.monitor_snapshot()
        self.assertEqual(snapshot["state"], "processing")
        self.assertEqual(snapshot["progress"], 0.5)
        # backendError не текущего бэкенда текущую нарезку не трогает
        job = types.SimpleNamespace(isCancelled=lambda: False, getError=lambda: "x", getResult=lambda: None)
        self.app.backend_manager.backends["CuraEngineBackend"].backendError.emit(job)
        self.assertNotIn("error", self.adapter.monitor_snapshot())
        # смена принтера (globalContainerStackChanged) меняет бэкенд без currendBackendChanged
        self.app.backend_manager.current = self.app.backend_manager.backends["CuraEngineBackend"]
        self.app.globalContainerStackChanged.emit()
        self.assertEqual(self.adapter.monitor_snapshot()["backend"], "CuraEngineBackend")

    def test_backend_error_messages(self):
        job = types.SimpleNamespace(isCancelled=lambda: False, getError=lambda: None,
                                    getResult=lambda: types.SimpleNamespace(name="NothingToSlice"))
        message, kind = self.adapter.describe_backend_error(job)
        self.assertEqual(kind, "nothing_to_slice")
        self.adapter.start_slicing()
        backend = self.app.getBackend()
        backend.backendStateChange.emit(4)
        backend.backendError.emit(job)
        snapshot = self.adapter.monitor_snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("Нечего слайсить", snapshot["error"])


if __name__ == "__main__":
    unittest.main()
