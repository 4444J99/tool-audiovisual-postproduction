"""Build a Blender scene from a set spec, headless.

    blender -b --factory-startup --python tools/blender_set_scene.py -- \
        --spec set.json --blend out/scene.blend --report out/report.json \
        [--still out/still.png --width 320 --height 180 --samples 16]
    blender -b --factory-startup --python tools/blender_set_scene.py -- --demo-spec out/spec.json

The spec (``amps.set_spec.v1`` or ``v1.1``) is validated and expanded to a scene graph by ``amp.studies.set_spec``
(pure Python); this script only copies the graph into Blender and then measures what Blender
actually holds, so the report states observed counts, dimensions, materials, lens and light
energy rather than echoing the input.  Exit status 2 means the spec failed validation and nothing
was written.  Run it under Blender's Python; ``tools/set_scene.py`` is the plain-Python front end.
"""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bmesh  # noqa: E402  (available only inside Blender)
import bpy  # noqa: E402  (available only inside Blender)
from mathutils import Vector  # noqa: E402

from amp.studies import set_spec  # noqa: E402


def parse(argv):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec")
    parser.add_argument("--demo-spec", metavar="PATH", help="write the synthetic spec here and exit")
    parser.add_argument("--demo-version", choices=("1", "1.1"), default="1")
    parser.add_argument("--blend")
    parser.add_argument("--report")
    parser.add_argument("--still")
    parser.add_argument("--width", type=int, default=320)
    parser.add_argument("--height", type=int, default=180)
    parser.add_argument("--samples", type=int, default=16)
    parser.add_argument("--camera", help="camera id to render; default is the spec's active camera")
    return parser.parse_args(argv[argv.index("--") + 1:] if "--" in argv else [])


def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for collection in list(bpy.data.collections):
        bpy.data.collections.remove(collection)
    return bpy.context.scene


def make_material(name, record):
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    bsdf = material.node_tree.nodes["Principled BSDF"]
    linear = record["linear"]
    bsdf.inputs["Base Color"].default_value = (linear[0], linear[1], linear[2], 1.0)
    bsdf.inputs["Roughness"].default_value = record["roughness"]
    if record["emission"] > 0:
        bsdf.inputs["Emission Color"].default_value = (linear[0], linear[1], linear[2], 1.0)
        bsdf.inputs["Emission Strength"].default_value = record["emission"]
    material["spec_hex"] = record["color"]
    return material


def look_at(obj, target):
    direction = Vector(target) - Vector(obj.location)
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def collection_for(scene, name):
    existing = bpy.data.collections.get(name)
    if existing is None:
        existing = bpy.data.collections.new(name)
        scene.collection.children.link(existing)
    return existing


def build(scene, graph):
    materials = {name: make_material(name, record) for name, record in graph["materials"].items()}
    scene.name = graph["title"]
    for entry in graph["objects"]:
        collection = collection_for(scene, entry["collection"])
        if entry["type"] == "mesh":
            mesh = bpy.data.meshes.new(entry["name"])
            mesh.from_pydata(entry["mesh"]["verts"], [], entry["mesh"]["faces"])
            mesh.update()
            for material_name in entry["materials"]:
                mesh.materials.append(materials[material_name])
            if entry["face_materials"] is not None:
                for polygon, index in zip(mesh.polygons, entry["face_materials"]):
                    polygon.material_index = index
            obj = bpy.data.objects.new(entry["name"], mesh)
            obj.location = entry["location"]
            obj.rotation_euler = (0.0, 0.0, math.radians(entry["rotation_deg"]))
            obj["role"] = entry["role"]
            collection.objects.link(obj)
        elif entry["type"] == "light":
            data = bpy.data.lights.new(entry["name"], {"point": "POINT", "sun": "SUN", "area": "AREA"}[entry["kind"]])
            data.energy = entry["energy"]
            data.color = tuple(set_spec.hex_to_linear(entry["color"]))
            if entry["kind"] == "area":
                data.size = entry["size"]
            obj = bpy.data.objects.new(entry["name"], data)
            obj.location = entry["location"]
            if entry["aim"] is not None:
                look_at(obj, entry["aim"])
            collection.objects.link(obj)
        else:
            data = bpy.data.cameras.new(entry["name"])
            data.lens = entry["lens_mm"]
            data.sensor_width = entry["sensor_width_mm"]
            data.sensor_fit = "HORIZONTAL"
            obj = bpy.data.objects.new(entry["name"], data)
            obj.location = entry["location"]
            look_at(obj, entry["aim"])
            collection.objects.link(obj)
    if graph["active_camera"]:
        scene.camera = bpy.data.objects[graph["active_camera"]]


def measure(scene, graph):
    """What Blender holds now, in the shape of ``set_spec.graph_summary``."""
    bpy.context.view_layer.update()
    rows = []
    for entry in graph["objects"]:
        obj = bpy.data.objects.get(entry["name"])
        if obj is None:
            rows.append({"name": entry["name"], "present": False})
            continue
        row = {"name": obj.name, "present": True, "type": entry["type"],
               "location": [round(v, 6) for v in obj.location],
               "collection": [c.name for c in obj.users_collection]}
        if obj.type == "MESH":
            world = [obj.matrix_world @ v.co for v in obj.data.vertices]
            held = bmesh.new()
            held.from_mesh(obj.data)
            row.update(world_bounds={"min": [round(min(p[i] for p in world), 6) for i in range(3)],
                                     "max": [round(max(p[i] for p in world), 6) for i in range(3)]},
                       volume=round(held.calc_volume(signed=True), 6),
                       watertight=all(len(edge.link_faces) == 2 for edge in held.edges))
            held.free()
            row.update(vertices=len(obj.data.vertices), faces=len(obj.data.polygons),
                       dimensions=[round(v, 6) for v in obj.dimensions],
                       materials=[m.name for m in obj.data.materials],
                       face_material_histogram={str(i): sum(1 for p in obj.data.polygons if p.material_index == i)
                                                for i in range(len(obj.data.materials))})
        elif obj.type == "LIGHT":
            row.update(light_type=obj.data.type, energy=obj.data.energy)
        elif obj.type == "CAMERA":
            row.update(lens_mm=obj.data.lens, sensor_width_mm=obj.data.sensor_width,
                       forward=[round(v, 4) for v in (obj.matrix_world.to_quaternion() @ Vector((0.0, 0.0, -1.0)))])
        rows.append(row)
    return {"objects": rows, "active_camera": scene.camera.name if scene.camera else None,
            "collections": sorted(c.name for c in bpy.data.collections),
            "blender_version": bpy.app.version_string,
            "mesh_count": sum(1 for o in bpy.data.objects if o.type == "MESH"),
            "light_count": sum(1 for o in bpy.data.objects if o.type == "LIGHT"),
            "camera_count": sum(1 for o in bpy.data.objects if o.type == "CAMERA")}


def render_still(scene, path, width, height, samples):
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    scene.cycles.use_denoising = False
    scene.render.resolution_x, scene.render.resolution_y = width, height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(path)
    scene.world = bpy.data.worlds.new("world")
    scene.world.color = (0.02, 0.02, 0.025)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.render.render(write_still=True)


def main():
    args = parse(sys.argv)
    if args.demo_spec:
        Path(args.demo_spec).parent.mkdir(parents=True, exist_ok=True)
        Path(args.demo_spec).write_text(json.dumps(set_spec.synthetic_spec(args.demo_version), indent=2, sort_keys=True) + "\n")
        return 0
    if not args.spec:
        print(json.dumps({"error": "give --spec or --demo-spec"}))
        return 2
    try:
        spec = json.loads(Path(args.spec).read_text())
    except (OSError, json.JSONDecodeError) as error:
        print(json.dumps({"error": f"cannot read spec: {error}"}))
        return 2
    if args.camera:
        if args.camera not in {c.get("id") for c in spec.get("cameras", []) if isinstance(c, dict)}:
            print(json.dumps({"error": f"camera {args.camera!r} is not in the spec"}))
            return 2
        spec = {**spec, "active_camera": args.camera}      # before expansion, so the graph hash covers the choice
    try:
        graph = set_spec.scene_graph(spec)
    except set_spec.SpecError as error:
        print(json.dumps({"errors": error.errors}, indent=2))
        return 2
    scene = reset_scene()
    build(scene, graph)
    observed = measure(scene, graph)
    expected = set_spec.graph_summary(graph)
    report = {"graph_sha256": graph["graph_sha256"], "expected": expected, "observed": observed}
    if args.still:
        render_still(scene, args.still, args.width, args.height, args.samples)
        report["still"] = {"path": str(args.still), "width": args.width, "height": args.height, "samples": args.samples}
    if args.blend:
        Path(args.blend).parent.mkdir(parents=True, exist_ok=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(args.blend))
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("SET_SCENE_OK " + json.dumps({"graph_sha256": graph["graph_sha256"], "meshes": observed["mesh_count"],
                                         "lights": observed["light_count"], "cameras": observed["camera_count"]}))
    return 0


if __name__ == "__main__":
    code = main()
    if code:
        sys.exit(code)
