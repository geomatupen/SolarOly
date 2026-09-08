import unittest

from pvrt.web.result_pagination import paginate_result_manifest


class ResultPaginationTests(unittest.TestCase):
    def test_limits_each_page_to_one_hundred_items(self):
        manifest = [{"file": f"image_{index}.png", "n": index % 3} for index in range(250)]

        first = paginate_result_manifest(
            manifest,
            page=1,
            page_size=100,
            detected_only=False,
        )
        last = paginate_result_manifest(
            manifest,
            page=3,
            page_size=100,
            detected_only=False,
        )

        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(first["page_count"], 3)
        self.assertEqual(first["total"], 250)
        self.assertEqual(len(last["items"]), 50)
        self.assertEqual(last["items"][0]["file"], "image_200.png")

    def test_applies_detection_filter_before_pagination(self):
        manifest = [{"file": f"image_{index}.png", "n": 1 if index % 2 else 0} for index in range(240)]

        result = paginate_result_manifest(
            manifest,
            page=2,
            page_size=100,
            detected_only=True,
        )

        self.assertEqual(result["unfiltered_total"], 240)
        self.assertEqual(result["detected_total"], 120)
        self.assertEqual(result["total"], 120)
        self.assertEqual(result["page_count"], 2)
        self.assertEqual(len(result["items"]), 20)
        self.assertTrue(all(item["n"] > 0 for item in result["items"]))

    def test_clamps_page_and_page_size(self):
        manifest = [{"file": "only.png", "n": 0}]

        result = paginate_result_manifest(
            manifest,
            page=99,
            page_size=500,
            detected_only=False,
        )

        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_size"], 100)


if __name__ == "__main__":
    unittest.main()
