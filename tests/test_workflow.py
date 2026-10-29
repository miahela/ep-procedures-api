"""Exercise exports and failure reporting with small offline API fixtures."""

import contextlib
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import requests

import ep_api_test as api


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.sitting_id = f"MTG-COM-AFET-{self.day}"
        env = {k: v for k, v in os.environ.items() if not k.startswith("EP_")}
        env.update(EP_OUTPUT_DIR=str(self.output), EP_REQUEST_DELAY="0", EP_MAX_PROCEDURES="1")
        env_patch = patch.dict(os.environ, env, clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def response(self, url, **kwargs):
        response = Mock()
        response.status_code = 200
        response.url = url
        response.content = b'{"data": []}'
        response.raise_for_status.return_value = None
        if url.endswith("/procedures/feed"):
            response.content = (
                b'<feed xmlns="http://www.w3.org/2005/Atom">'
                b'<entry><id>https://example.test/procedure/2026-0001</id></entry></feed>'
            )
            return response
        if url.endswith("/procedures/2026-0001"):
            item = {
                "process_title": {"en": "Fixture procedure"},
                "process_type": "def/ep-procedure-types/COD",
                "consists_of": [{"activity_id": "event/1", "had_activity_type": "def/ep-activities/debate"}],
                "had_participation": [{
                    "id": "participation/1", "had_participant_person": ["person/123"],
                    "had_participant_organization": ["org/AFET"],
                }],
            }
        elif url.endswith("/meps/123"):
            item = {"label": "Fixture MEP", "nationality": "XX"}
        elif url.endswith("/meetings"):
            item = {"activity_id": self.sitting_id, "label": "Fixture meeting"}
        elif url.endswith("/foreseen-activities"):
            item = {"activity_id": "agenda/1", "process_id": "procedure/2026-0001"}
        else:
            raise AssertionError(f"Unexpected request: {url}")
        response.json.return_value = {"data": [item]}
        return response

    def run_workflow(self, failure_suffix=None):
        def get(url, **kwargs):
            if failure_suffix and url.endswith(failure_suffix):
                response = self.response(url, **kwargs)
                response.status_code = 503
                response.raise_for_status.side_effect = requests.HTTPError("Fixture upstream failure")
                return response
            return self.response(url, **kwargs)

        with patch.object(api.requests, "get", side_effect=get):
            with contextlib.redirect_stdout(io.StringIO()):
                api.main()
        return json.loads((self.output / "test_report.json").read_text())

    def test_exports_and_cross_reference_are_preserved(self):
        report = self.run_workflow()
        self.assertTrue(all(row["status"] == "PASS" for row in report))
        meta = json.loads((self.output / "procedures_meta.json").read_text())
        self.assertEqual(meta[0]["proc_id"], "2026-0001")
        self.assertIn("Fixture MEP", (self.output / "participations_resolved.csv").read_text())
        self.assertIn("2026-0001", (self.output / "foreseen_cross_refs.csv").read_text())
        self.assertEqual(api.request_count, 5)

    def test_failed_meetings_or_agenda_are_not_reported_as_success(self):
        for suffix, check in [
            ("/meetings", "Meetings endpoint: AFET"),
            ("/foreseen-activities", f"Foreseen activities: {self.sitting_id}"),
        ]:
            with self.subTest(suffix=suffix):
                report = self.run_workflow(suffix)
                row = next(row for row in report if row["check"] == check)
                self.assertEqual(row["status"], "FAIL")
                self.assertEqual(row["detail"], "Request failed")

    def test_successful_empty_response_differs_from_bad_json_or_timeout(self):
        api.REQ_DELAY = 0
        response = Mock()
        response.status_code = 200
        response.url = "https://example.test/meetings"
        response.content = b'{"data": []}'
        response.raise_for_status.return_value = None
        with patch.object(api.requests, "get", return_value=response):
            response.json.return_value = {"data": []}
            self.assertEqual(api.fetch_meetings("AFET", "2026-01-01", "2026-01-02"), [])
            with contextlib.redirect_stdout(io.StringIO()):
                for data in [[], {"data": "bad"}, {"data": [1]}]:
                    response.json.return_value = data
                    self.assertIsNone(api.fetch_meetings("AFET", "2026-01-01", "2026-01-02"))
        before = api.request_count
        with patch.object(api.requests, "get", side_effect=requests.Timeout):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(api.get_json("https://example.test"))
        self.assertEqual(api.request_count, before + 1)

    def test_corrupt_cache_fails_before_api_requests(self):
        for value in ["{broken", "[]", '{"123": "bad entry"}']:
            with self.subTest(value=value):
                (self.output / "mep_cache.json").write_text(value)
                with patch.object(api.requests, "get") as get:
                    with contextlib.redirect_stdout(io.StringIO()):
                        with self.assertRaisesRegex(ValueError, "cache"):
                            api.main()
                    get.assert_not_called()
                self.assertEqual((self.output / "mep_cache.json").read_text(), value)

    def test_ignored_server_filters_keep_only_requested_committee_and_id_dates(self):
        rows = [
            {"activity_id": f"MTG-PL-{2014 + i % 13}-10-29"}
            for i in range(708)
        ] + [
            {"activity_id": "MTG-COM-BUDG-2026-10-29", "activity_date": "2014-01-01"},
            {"activity_id": "MTG-COM-2026-10-30", "had_participant_organization": ["org/BUDG"]},
            {"activity_id": "MTG-COM-BUDG-2026-10-28"},
            {"activity_id": "MTG-COM-BUDG-2026-10-31"},
            {"activity_id": "MTG-COM-AFET-2026-10-29"},
            {"activity_id": "MTG-PL-2026-10-29", "body_id": "BUDG"},
            {"activity_id": "MTG-COM-BUDG-undated"},
            {"activity_id": "MTG-COM-BUDG-2026-99-29"},
        ]
        response = self.response("https://example.test/meetings")
        response.json.return_value = {"data": rows}
        with patch.object(api.requests, "get", return_value=response) as get:
            with contextlib.redirect_stdout(io.StringIO()):
                meetings = api.fetch_meetings("BUDG", "2026-10-29", "2026-10-30")
        self.assertEqual({m["activity_id"] for m in meetings}, {
            "MTG-COM-BUDG-2026-10-29", "MTG-COM-2026-10-30",
        })
        self.assertEqual(get.call_args.kwargs["params"], {
            "format": "application/ld+json", "year": "2026", "offset": 0, "limit": 50,
        })
        with patch.object(api.requests, "get", return_value=response) as get:
            with contextlib.redirect_stdout(io.StringIO()):
                api.fetch_meetings("BUDG", "2026-12-31", "2027-01-01")
        self.assertEqual(get.call_args.kwargs["params"]["year"], "2026,2027")

    def test_708_plenary_meetings_trigger_no_agenda_calls(self):
        def get(url, **kwargs):
            response = self.response(url, **kwargs)
            if url.endswith("/meetings"):
                response.json.return_value = {"data": [
                    {"activity_id": f"MTG-PL-{2014 + i % 13}-10-29"} for i in range(708)
                ]}
            return response

        with patch.object(api.requests, "get", side_effect=get) as requests_get:
            with contextlib.redirect_stdout(io.StringIO()):
                api.main()
        self.assertFalse(any(call.args[0].endswith("/foreseen-activities") for call in requests_get.call_args_list))
        report = json.loads((self.output / "test_report.json").read_text())
        info = next(r for r in report if r["check"] == "Foreseen activities collected (informational)")
        self.assertEqual(info["status"], "PASS")

    def test_default_meeting_cap_limits_708_accepted_rows_to_20_agenda_calls(self):
        def get(url, **kwargs):
            response = self.response(url, **kwargs)
            if url.endswith("/meetings"):
                response.json.return_value = {"data": [
                    {"activity_id": f"{self.sitting_id}-{i:04d}"} for i in range(708)
                ]}
            return response

        with patch.object(api.requests, "get", side_effect=get) as requests_get:
            with contextlib.redirect_stdout(io.StringIO()):
                api.main()
        agenda = [c for c in requests_get.call_args_list if c.args[0].endswith("/foreseen-activities")]
        self.assertEqual(api.MAX_MEETINGS, 20)
        self.assertEqual(len(agenda), 20)
        with (self.output / "meetings.csv").open(newline="") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 20)

    def test_meeting_cap_is_global_across_committees(self):
        os.environ["EP_MAX_MEETINGS"] = "3"

        def get(url, **kwargs):
            response = self.response(url, **kwargs)
            data = response.json.return_value
            if "/procedures/2026-0001" in url:
                data["data"][0]["had_participation"].append({
                    "id": "participation/2", "had_participant_organization": ["org/BUDG"],
                })
            if url.endswith("/meetings"):
                response.json.return_value = {"data": [
                    {"activity_id": f"MTG-COM-{body}-{self.day}-{i}"}
                    for body in ["AFET", "BUDG"] for i in range(2)
                ]}
            return response

        with patch.object(api.requests, "get", side_effect=get) as requests_get:
            with contextlib.redirect_stdout(io.StringIO()):
                api.main()
        agenda = [c for c in requests_get.call_args_list if c.args[0].endswith("/foreseen-activities")]
        self.assertEqual(len(agenda), 3)
        self.assertEqual(sum("BUDG" in c.args[0] for c in agenda), 1)

    def test_meeting_cap_rejects_zero_negative_and_fractional_values(self):
        for value in ["0", "-1", "1.5"]:
            with self.subTest(value=value):
                os.environ["EP_MAX_MEETINGS"] = value
                with self.assertRaisesRegex(ValueError, "EP_MAX_MEETINGS"):
                    api.configure()

    def test_agenda_status_and_empty_body_classification(self):
        # Empty/unsupported responses are zero items; 5xx fails even with no body.
        for status, body in [(200, b""), (200, b" \n"), (204, b""), (404, b"<html>missing</html>"), (403, b"denied")]:
            with self.subTest(status=status, body=body):
                response = self.response("https://example.test/foreseen-activities")
                response.status_code, response.content = status, body
                with patch.object(api.requests, "get", return_value=response):
                    with contextlib.redirect_stdout(io.StringIO()) as console:
                        self.assertEqual(api.fetch_foreseen_activities(self.sitting_id), [])
                self.assertIn(f"HTTP {status}", console.getvalue())
                response.json.assert_not_called()
        for status, body in [(500, b""), (503, b"error"), (200, b"<html>not JSON</html>")]:
            with self.subTest(status=status, body=body):
                response = self.response("https://example.test/foreseen-activities")
                response.status_code, response.content = status, body
                response.json.side_effect = ValueError("Expecting value: line 1 column 1")
                with patch.object(api.requests, "get", return_value=response):
                    with contextlib.redirect_stdout(io.StringIO()) as console:
                        self.assertIsNone(api.fetch_foreseen_activities(self.sitting_id))
                self.assertIn(f"HTTP {status}", console.getvalue())
        with patch.object(api.requests, "get", side_effect=requests.Timeout):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(api.fetch_foreseen_activities(self.sitting_id))


if __name__ == "__main__":
    unittest.main()
