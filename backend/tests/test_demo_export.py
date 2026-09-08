import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from pvrt.web.demo_export import create_solar_demo_export, delete_solar_demo_export


class DemoExportTests(unittest.TestCase):
    def test_creates_expected_archive_and_requires_replace_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sessions = root / "outputs"
            job_dir = sessions / ".postprocess_jobs" / "demo-job"
            segmentation_workspace = job_dir / "snapshots" / "segmentation"
            anomaly_workspace = job_dir / "snapshots" / "anomaly"
            segmentation_workflow = segmentation_workspace / "postprocess" / "panels"
            anomaly_workflow = anomaly_workspace / "postprocess" / "anomalies"
            anomaly_result = sessions / "anomaly-result"
            rotated_images = anomaly_result / "rotated_images"
            for directory in (segmentation_workflow, anomaly_workflow, rotated_images):
                directory.mkdir(parents=True)

            vector_feature = {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[8.04, 49.04], [8.06, 49.04], [8.06, 49.06], [8.04, 49.04]]],
                },
                "properties": {},
            }
            anomaly_feature = json.loads(json.dumps(vector_feature))
            anomaly_feature["properties"] = {"image": "panel_1", "anomaly_id": "A-1"}
            image_feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [8.05, 49.05]},
                "properties": {
                    "image": "panel_1.jpeg",
                    "src": "panel_1.jpeg",
                    "prepared_image": "/api/project_file/absolute/path/panel_1.png",
                    "overlay": "/api/project_file/absolute/path/panel_1.png",
                    "w": 999,
                    "h": 999,
                    "meters_per_pixel": 0.025,
                    "rotation": -1.25,
                    "corners": [[8.0, 49.1], [8.1, 49.1], [8.1, 49.0], [8.0, 49.0]],
                    "lens_correction_status": "corrected",
                    "row_alignment_status": "aligned",
                    "row_alignment_method": "lightglue_pose_graph",
                },
            }
            unaligned_image_feature = {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [8.25, 49.05]},
                "properties": {
                    "image": "panel_2.jpeg",
                    "w": 8,
                    "h": 6,
                    "meters_per_pixel": 0.03,
                    "rotation": 0,
                    "corners": [[8.2, 49.1], [8.3, 49.1], [8.3, 49.0], [8.2, 49.0]],
                },
            }
            vector_collection = {"type": "FeatureCollection", "features": [vector_feature]}
            anomaly_collection = {"type": "FeatureCollection", "features": [anomaly_feature]}
            image_collection = {
                "type": "FeatureCollection",
                "features": [image_feature, unaligned_image_feature],
            }
            panels = segmentation_workflow / "regularized.geojson"
            rows = segmentation_workflow / "solar_rows.geojson"
            anomalies = anomaly_workflow / "associated.geojson"
            images = anomaly_result / "images.geojson"
            for path in (panels, rows):
                path.write_text(json.dumps(vector_collection), encoding="utf-8")
            anomalies.write_text(json.dumps(anomaly_collection), encoding="utf-8")
            images.write_text(json.dumps(image_collection), encoding="utf-8")
            image_path = rotated_images / "panel_1.png"
            Image.new("RGBA", (10, 12), (255, 0, 0, 255)).save(image_path)
            image_bytes = image_path.read_bytes()
            Image.new("RGBA", (8, 6), (0, 255, 0, 255)).save(rotated_images / "panel_2.png")

            (job_dir / "job.json").write_text(json.dumps({
                "sources": {"anomaly": {"result_id": "anomaly-result"}},
                "workflows": {
                    "segmentation": {"workflow_id": "panels"},
                    "anomaly": {"workflow_id": "anomalies"},
                },
            }), encoding="utf-8")
            (segmentation_workflow / "status.json").write_text(json.dumps({
                "status": "complete",
                "outputs": {
                    "regularized": {"path": "postprocess/panels/regularized.geojson"},
                    "solar_rows": {"path": "postprocess/panels/solar_rows.geojson"},
                },
            }), encoding="utf-8")
            (anomaly_workflow / "status.json").write_text(json.dumps({
                "status": "complete",
                "outputs": {"associated": {"path": "postprocess/anomalies/associated.geojson"}},
            }), encoding="utf-8")

            result = create_solar_demo_export(job_dir, sessions)

            archive_path = Path(result["path"])
            self.assertTrue(archive_path.is_file())
            self.assertEqual(result["anomaly_count"], 1)
            self.assertEqual(result["image_count"], 2)
            self.assertEqual(
                result["images_geojson"],
                "anomaly_overlays/images_finalized.geojson",
            )
            self.assertEqual(result["validated_anomaly_count"], 1)
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "vector/solar_panels.geojson",
                    "vector/solar_rows.geojson",
                    "vector/anomalies.geojson",
                    "anomaly_overlays/images_finalized.geojson",
                    "anomaly_overlays/panel_1.png",
                    "anomaly_overlays/panel_2.png",
                })
                self.assertEqual(archive.read("anomaly_overlays/panel_1.png"), image_bytes)
                finalized = json.loads(archive.read("anomaly_overlays/images_finalized.geojson"))
                self.assertEqual(finalized["export_schema"], "solaroly.image-placement.v1")
                self.assertEqual(finalized["pixel_origin"], "top_left")
                self.assertEqual(
                    finalized["corner_order"],
                    ["top_left", "top_right", "bottom_right", "bottom_left"],
                )
                self.assertEqual(finalized["validation"]["referenced_anomaly_count"], 1)
                self.assertEqual(finalized["validation"]["image_count"], 2)
                exported_by_id = {feature["id"]: feature for feature in finalized["features"]}
                exported = exported_by_id["panel_1"]
                self.assertEqual(exported["id"], "panel_1")
                self.assertEqual(exported["properties"]["image"], "panel_1.png")
                self.assertEqual(exported["properties"]["file"], "panel_1.png")
                self.assertEqual(exported["properties"]["source_image"], "panel_1.jpeg")
                self.assertEqual(exported["properties"]["width"], 10)
                self.assertEqual(exported["properties"]["height"], 12)
                self.assertEqual(exported["properties"]["effective_meters_per_pixel"], 0.025)
                self.assertEqual(exported["properties"]["map_rotation_deg"], -1.25)
                self.assertFalse(exported["properties"]["map_rotation_baked_into_pixels"])
                self.assertEqual(exported["properties"]["alignment_mode"], "lightglue")
                self.assertTrue(exported["properties"]["lens_corrected"])
                self.assertNotIn("prepared_image", exported["properties"])
                self.assertNotIn("overlay", exported["properties"])
                unaligned = exported_by_id["panel_2"]
                self.assertEqual(unaligned["properties"]["alignment_mode"], "none")
                self.assertEqual(unaligned["properties"]["alignment_status"], "not_requested")
                self.assertTrue(unaligned["properties"]["map_rotation_baked_into_pixels"])
                self.assertFalse(unaligned["properties"]["lens_corrected"])

            with self.assertRaises(FileExistsError):
                create_solar_demo_export(job_dir, sessions)
            replaced = create_solar_demo_export(job_dir, sessions, replace=True)
            self.assertEqual(replaced["path"], str(archive_path))
            deleted = delete_solar_demo_export(job_dir)
            self.assertEqual(deleted["path"], str(archive_path))
            self.assertGreater(deleted["deleted_size"], 0)
            self.assertFalse(archive_path.exists())
            with self.assertRaises(FileNotFoundError):
                delete_solar_demo_export(job_dir)


if __name__ == "__main__":
    unittest.main()
