"""Synthetic fixtures live only in TemporaryDirectory, never in production data."""
import copy
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="topic-assistant-test-")
        self.root = Path(self.temp.name)
        app.atomic_json(self.root / "config.json", {"target_thread": "test-task"})
        self.store = app.Store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, key="test"):
        request = self.store.create(key, queue=False)["request"]
        self.store.claim(request["id"], "test-task")
        return request["id"]

    def video(self, vid="video1", comments=1, days_ago=1, group="cq_xhs"):
        day = (datetime.now(app.TZ) - timedelta(days=days_ago)).date().isoformat()
        return {
            "platform": "xhs", "group": group, "video_id": vid,
            "url": "https://www.xiaohongshu.com/explore/" + vid + "?tracking=test",
            "title": "测试视频（仅临时目录）", "author": "测试发布者" + vid,
            "observed_at": app.now(), "likes": {"value": 12000, "raw": "1.2万", "approximate": True},
            "comment_count": None, "published_date": day, "date_raw": day, "published_date_basis": "可见完整发布日期",
            "video_evidence": {"type": "controls", "detail": "测试播放控件", "duration_seconds": 30},
            "search": {"query": "测试种子", "filters": ["视频"], "sort": "默认", "scrolls": 1, "read_count": comments},
            "comments": [{"comment_id": "%024x" % i, "text": "测试评论%d：物业维修费用怎么看" % i,
                          "author": "测试用户%d" % i, "likes": None, "date": day, "date_raw": day,
                          "date_basis": "可见完整评论日期"} for i in range(comments)],
        }

    def semantic(self, videos, refs=None):
        refs = refs or [(0, 0), (1, 0)]
        return {"group": "cq_xhs", "label": "维修费用", "comment_refs": [
            {"platform": "xhs", "video_id": videos[vi]["video_id"], "comment_id": videos[vi]["comments"][ci]["comment_id"], "quote": videos[vi]["comments"][ci]["text"]}
            for vi, ci in refs], "topic": {"title": "维修费用该问哪些问题", "angle": "从原评论提出核验问题", "outline": ["明确费用", "核对公示", "实地查证"]}}

    def test_exact_xhs_id_rule_and_no_cross_video_merge(self):
        bare = "ABCDEF123456ABCDEF123456"
        self.assertEqual(app.normalize_comment_id("xhs", "comment-" + bare), bare.lower())
        self.assertEqual(app.normalize_comment_id("xhs", bare), bare.lower())
        self.assertEqual(app.normalize_comment_id("xhs", "comment-ab12"), "comment-ab12")
        self.assertEqual(app.normalize_comment_id("douyin", "comment-" + bare), "comment-" + bare)
        self.assertNotEqual(app.comment_key("xhs", "v1", bare), app.comment_key("xhs", "v2", bare))

    def test_visible_url_ids_canonicalize_without_merging_other_videos(self):
        self.assertEqual(app.canonical_url("https://www.douyin.com/jingxuan/search/重庆?modal_id=12345678&other=x"), "https://www.douyin.com/video/12345678")
        self.assertNotEqual(app.video_key("douyin", None, "https://www.douyin.com/jingxuan/search/重庆?modal_id=123"), app.video_key("douyin", None, "https://www.douyin.com/jingxuan/search/重庆?modal_id=124"))
        self.assertEqual(app.video_key("douyin", None, "https://www.douyin.com/jingxuan/search/重庆?modal_id=123"), app.video_key("douyin", "123", "https://www.douyin.com/video/123"))
        note = "abcdef123456abcdef123456"
        self.assertEqual(app.canonical_url("https://www.xiaohongshu.com/search_result/" + note + "?xsec_token=test"), "https://www.xiaohongshu.com/explore/" + note)
        self.assertEqual(app.video_key("xhs", None, "https://www.xiaohongshu.com/search_result/" + note), app.video_key("xhs", note, "https://www.xiaohongshu.com/explore/" + note))
        with self.assertRaises(ValueError):
            app.video_key("douyin", "456", "https://www.douyin.com/video/123")

    def test_week_ranges_must_fit_entirely_inside_recent_window(self):
        day = datetime.now(app.TZ).date()
        three_weeks = {"start": (day-timedelta(days=28)).isoformat(), "end": (day-timedelta(days=21)).isoformat(), "basis": "页面3周前，按含边界日期的完整区间换算"}
        four_weeks = {"start": (day-timedelta(days=35)).isoformat(), "end": (day-timedelta(days=28)).isoformat(), "basis": "页面4周前，按含边界日期的完整区间换算"}
        self.assertTrue(app.is_recent({"date": None, "date_range": three_weeks}, app.now()))
        self.assertFalse(app.is_recent({"date": day.isoformat(), "date_basis": "可见完整评论日期", "date_range": four_weeks}, app.now()))
        three_weeks["basis"] = "推测范围，待核实"
        self.assertFalse(app.is_recent({"date_range": three_weeks}, app.now()))

    def test_date_is_preserved_and_counts_distinguish_reread(self):
        rid = self.request()
        video = self.video()
        original_date = video["comments"][0]["date"]
        self.store.ingest(rid, {"videos": [video], "batch_id": "a"})
        review = copy.deepcopy(video)
        review["comments"][0].update(comment_id="comment-" + review["comments"][0]["comment_id"], date=None, date_basis=None, date_raw=None)
        self.store.ingest(rid, {"videos": [review], "batch_id": "b"})
        report = self.store.public()
        self.assertEqual(report["comments"][0]["date"], original_date)
        self.assertEqual(report["counts"]["read"], 2)
        self.assertEqual(report["counts"]["new"], 1)
        self.assertEqual(report["counts"]["duplicates"], 1)
        self.assertEqual(report["counts"]["independent_comments"], 1)
        self.store.finish(rid, "partial", "测试部分保存")
        later = self.request("next")
        self.store.ingest(later, {"videos": [review]})
        self.assertEqual(self.store.public()["counts"]["new"], 0)
        self.assertEqual(self.store.public()["counts"]["duplicates"], 1)

    def test_ranking_counts_explicit_ids_only(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 10) for i in range(3)]
        semantics = self.semantic(videos)
        semantics["comment_refs"].append(copy.deepcopy(semantics["comment_refs"][0]))
        self.store.ingest(rid, {"videos": videos, "semantic_groups": [semantics]})
        report = self.store.public()
        keyword = report["keywords"][0]
        self.assertEqual(keyword["status"], "ranked")
        self.assertEqual(keyword["comment_count"], 2)  # same words in all other comments do not expand membership
        self.assertEqual(keyword["video_count"], 2)
        self.assertEqual(report["counts"]["recent_comments"], 30)
        self.assertIsNone(keyword["like_sum"])
        self.assertEqual(len(report["topics"]), 1)

    def test_recent_threshold_and_second_video_requirement(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 10) for i in range(3)]
        videos[2]["comments"][0].update(date=(datetime.now(app.TZ)-timedelta(days=31)).date().isoformat())
        self.store.ingest(rid, {"videos": videos, "semantic_groups": [self.semantic(videos)]})
        self.assertEqual(self.store.public()["keywords"][0]["status"], "exploratory")
        videos[2]["comments"][0].update(date=(datetime.now(app.TZ)-timedelta(days=1)).date().isoformat())
        one_video_semantic = self.semantic(videos, [(0, 0), (0, 1)])
        self.store.ingest(rid, {"videos": [videos[2]], "semantic_groups": [one_video_semantic]})
        report = self.store.public()
        self.assertEqual(report["counts"]["recent_comments"], 30)
        self.assertEqual(report["keywords"][0]["status"], "exploratory")

    def test_unknown_and_edited_dates_never_become_recent(self):
        day = datetime.now(app.TZ).date().isoformat()
        for basis in (None, "待核实", "编辑日期", "年份未显示", "推测年份", "不确定", "visible updated date", "missing year"):
            self.assertIsNone(app.reliable_date(day, basis))
        self.assertIsNone(app.reliable_date("09-13", "页面仅显示月日"))
        rid = self.request()
        video = self.video()
        video["comments"][0].update(date=day, date_basis="年份未显示，近期性待核实")
        self.store.ingest(rid, {"videos": [video]})
        self.assertIsNone(self.store.public()["comments"][0]["date"])
        self.assertEqual(self.store.public()["counts"]["recent_comments"], 0)

    def test_invalid_quotes_and_ids_cannot_support_topics(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 1) for i in range(2)]
        semantic = self.semantic(videos)
        semantic["comment_refs"][0]["quote"] = "视频标题不能代替评论"
        semantic["comment_refs"][1]["comment_id"] = "does-not-exist"
        result = self.store.ingest(rid, {"videos": videos, "semantic_groups": [semantic]})
        self.assertGreaterEqual(len(result["validation_errors"]), 2)
        self.assertEqual(self.store.public()["keywords"], [])
        self.assertEqual(self.store.public()["topics"], [])

    def test_topic_refs_are_a_verified_subset_and_semantic_update_does_not_reread(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 10) for i in range(3)]
        semantic = self.semantic(videos, [(0, 0), (1, 0), (2, 0)])
        self.store.ingest(rid, {"batch_id": "comments", "videos": videos, "semantic_groups": [semantic]})
        before = self.store.public()["counts"]
        semantic["topic"]["comment_refs"] = [semantic["comment_refs"][0]]
        self.store.ingest(rid, {"batch_id": "topic-selection", "semantic_groups": [semantic]})
        report = self.store.public()
        self.assertEqual(report["keywords"][0]["comment_count"], 3)
        self.assertEqual(len(report["topics"][0]["comment_refs"]), 1)
        self.assertEqual(report["topics"][0]["comment_refs"][0]["video_id"], "v0")
        for name in ("read", "new", "duplicates", "independent_comments"):
            self.assertEqual(report["counts"][name], before[name])
        semantic["topic"]["comment_refs"] = []
        result = self.store.ingest(rid, {"semantic_groups": [semantic]})
        self.assertTrue(any("不生成选题" in s for s in result["validation_errors"]))
        self.assertEqual(self.store.public()["topics"], [])

    def test_topic_refs_reject_out_of_group_and_wrong_quotes(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 3) for i in range(3)]
        semantic = self.semantic(videos, [(0, 0), (1, 0)])
        outside = self.semantic(videos, [(2, 0)])["comment_refs"][0]
        wrong = copy.deepcopy(semantic["comment_refs"][0])
        wrong["quote"] = "不匹配原文"
        semantic["topic"]["comment_refs"] = [outside, wrong]
        result = self.store.ingest(rid, {"videos": videos, "semantic_groups": [semantic]})
        self.assertGreaterEqual(len(result["validation_errors"]), 3)
        self.assertEqual(len(self.store.public()["keywords"]), 1)
        self.assertEqual(self.store.public()["topics"], [])

    def test_exclusions_and_video_gate(self):
        rid = self.request()
        video = self.video(comments=5)
        video["comments"][0]["is_author"] = True
        video["comments"][1]["is_platform_ai"] = True
        video["comments"][2]["text"] = "😀😍🔥"
        video["comments"][3]["comment_id"] = None
        self.store.ingest(rid, {"videos": [video]})
        report = self.store.public()
        self.assertEqual(report["counts"]["read"], 5)
        self.assertEqual(report["counts"]["valid_comments"], 1)
        self.assertEqual(report["counts"]["unknown_id_comments"], 1)
        video["video_evidence"] = {"type": "image", "detail": "图文"}
        self.store.ingest(rid, {"videos": [video]})
        self.assertEqual(self.store.public()["counts"]["valid_comments"], 0)
        self.assertEqual(self.store.public()["counts"]["videos"], 0)
        self.assertIsNone(app.metric({"value": 0, "raw": None}))
        self.assertTrue(app.metric({"value": 12000, "raw": "1.2万"})["approximate"])

    def test_idempotency_claim_cancel_expire_and_batch(self):
        first = self.store.create("one", queue=False)
        self.assertTrue(self.store.create("two", queue=False)["reused"])
        rid = first["request"]["id"]
        with self.assertRaises(ValueError):
            self.store.claim(rid, "wrong-task")
        self.store.claim(rid, "test-task")
        with self.assertRaises(ValueError):
            self.store.claim(rid, "test-task")
        payload = {"videos": [self.video()], "batch_id": "unique-batch"}
        self.store.ingest(rid, payload)
        self.assertTrue(self.store.ingest(rid, payload)["reused"])
        self.assertEqual(self.store.public()["counts"]["read"], 1)
        self.store.stop(rid)
        self.assertTrue(self.store.create("one", queue=False)["reused"])
        with self.assertRaises(ValueError):
            self.store.ingest(rid, payload)
        with self.assertRaises(ValueError):
            self.store.claim(rid, "test-task")
        self.assertEqual(self.store.history(rid)["counts"]["new"], 1)
        expiring = self.store.create("later", queue=False)["request"]["id"]
        with self.store.transaction() as state:
            state["requests"][expiring]["expires_at"] = (datetime.now(app.TZ)-timedelta(seconds=1)).isoformat()
            self.store.save(state)
        with self.assertRaises(ValueError):
            self.store.claim(expiring, "test-task")
        self.assertEqual(self.store.history(expiring)["status"], "expired")

    def test_finished_request_and_saved_history_survive_reload(self):
        rid = self.request()
        self.store.ingest(rid, {"videos": [self.video()]})
        self.assertEqual(app.Store(self.root).history(rid)["counts"]["new"], 1)
        self.store.finish(rid, "partial", "只有一个视频")
        with self.assertRaises(ValueError):
            self.store.finish(rid, "completed")
        reopened = app.Store(self.root)
        self.assertEqual(reopened.public()["history"][0]["status"], "partial")
        self.assertEqual(reopened.public()["counts"]["independent_comments"], 1)
        self.assertTrue((self.root / "data" / "requests" / (rid + ".json")).exists())

    def test_new_request_never_overwrites_historical_evidence_or_dates(self):
        first = self.request()
        video = self.video()
        self.store.ingest(first, {"videos": [video]})
        self.store.finish(first, "partial", "首轮")
        saved = self.store.history(first)
        second = self.request("second")
        self.assertEqual(self.store.history(first), saved)
        changed = copy.deepcopy(video)
        changed["comments"][0].update(text="复查到的编辑后评论", date=None, date_raw=None, date_basis=None)
        self.store.ingest(second, {"videos": [changed]})
        self.assertEqual(self.store.history(first), saved)
        self.assertEqual(self.store.public()["comments"][0]["date"], video["comments"][0]["date"])
        self.assertEqual(self.store.public()["counts"]["new"], 0)

    def test_video_cannot_be_reclassified_or_double_counted_across_groups(self):
        rid = self.request()
        video = self.video(group="cq_xhs")
        self.store.ingest(rid, {"videos": [video]})
        changed = copy.deepcopy(video)
        changed.update(group="cn_xhs", title="试图覆盖原标题")
        result = self.store.ingest(rid, {"videos": [changed]})
        self.assertTrue(any("跨组" in error for error in result["validation_errors"]))
        report = self.store.public()
        self.assertEqual(report["counts"]["videos"], 1)
        self.assertEqual(report["counts"]["read"], 1)
        self.assertEqual(report["videos"][0]["title"], video["title"])
        self.assertEqual(report["videos"][0]["group"], "cq_xhs")
        self.store.finish(rid, "partial")
        later = self.request("later-group")
        self.store.ingest(later, {"videos": [changed]})
        self.assertEqual(self.store.public()["counts"]["videos"], 0)

    def test_changed_quote_requires_new_semantic_assignment(self):
        rid = self.request()
        videos = [self.video("v%d" % i, 10) for i in range(3)]
        self.store.ingest(rid, {"videos": videos, "semantic_groups": [self.semantic(videos)]})
        self.assertEqual(self.store.public()["keywords"][0]["comment_count"], 2)
        videos[1]["comments"][0]["text"] = "编辑后的原文已经讨论另一个问题"
        self.store.ingest(rid, {"videos": [videos[1]]})
        self.assertEqual(self.store.public()["keywords"][0]["comment_count"], 1)
        self.assertEqual(self.store.public()["keywords"][0]["status"], "exploratory")

    def test_atomic_write_failure_keeps_original_and_cleans_temp(self):
        target = self.root / "atomic.json"
        app.atomic_json(target, {"previous": True})
        with patch("app.os.replace", side_effect=OSError("test failure")):
            with self.assertRaises(OSError):
                app.atomic_json(target, {"next": True})
        self.assertEqual(json.loads(target.read_text()), {"previous": True})
        self.assertEqual(list(self.root.glob(".atomic.json.*.tmp")), [])

    def test_failed_state_commit_cannot_publish_uncommitted_history(self):
        rid = self.request()
        self.store.ingest(rid, {"videos": [self.video()]})
        original = self.store.history(rid)
        with patch.object(self.store, "save", side_effect=OSError("test state commit failure")):
            with self.assertRaises(OSError):
                self.store.ingest(rid, {"videos": [self.video("second")]})
        self.assertEqual(self.store.public()["counts"]["new"], 1)
        self.assertEqual(self.store.history(rid), original)

    def test_committed_result_survives_report_mirror_failure(self):
        rid = self.request()
        original_atomic = app.atomic_json

        def fail_report(path, value):
            if Path(path).parent.name == "reports":
                raise OSError("test report mirror failure")
            return original_atomic(path, value)

        with patch("app.atomic_json", side_effect=fail_report):
            self.store.ingest(rid, {"videos": [self.video()], "batch_id": "committed"})
        reopened = app.Store(self.root)
        self.assertEqual(reopened.public()["counts"]["new"], 1)
        self.assertEqual(reopened.history(rid)["counts"]["new"], 1)
        self.assertTrue(reopened.ingest(rid, {"videos": [self.video()], "batch_id": "committed"})["reused"])
        reopened.progress(rid, "flush committed report")
        self.assertTrue((self.root / "data" / "reports" / (rid + ".json")).exists())

    def test_missing_observation_time_stays_unknown(self):
        rid = self.request()
        video = self.video()
        video.pop("observed_at")
        self.store.ingest(rid, {"videos": [video]})
        report = self.store.public()
        self.assertIsNone(report["videos"][0]["observed_at"])
        self.assertIsNone(report["comments"][0]["observed_at"])
        self.assertIsNotNone(report["comments"][0]["ingested_at"])

    def test_metrics_require_a_matching_visible_number(self):
        self.assertIsNone(app.metric({"value": 1000, "raw": "赞"}))
        self.assertIsNone(app.metric({"value": 1000, "raw": "未知"}))
        self.assertIsNone(app.metric({"value": 1000, "raw": "100"}))
        self.assertIsNone(app.metric({"value": 12000, "raw": "1.2"}))
        self.assertEqual(app.metric({"value": 1234, "raw": "1,234"})["value"], 1234)
        self.assertEqual(app.metric({"value": 12000, "raw": "点赞 1.2万"})["value"], 12000)

    def test_raw_archive_and_observation_boundaries_are_preserved(self):
        rid = self.request()
        video = self.video()
        video.update(observed_at_basis="浏览器预检时观察，早于入库", selection_note="人工选取可见视频", observed_url=video["url"])
        video["comments"][0].update(emoji_images=["[捂脸]"], text_note="文字部分与表情图片分开保存", comment_id_basis="可见DOM ID经复查稳定")
        payload = {"batch_id": "raw-archive", "videos": [video], "unmodeled_boundary": "也必须保存在原始输入"}
        result = self.store.ingest(rid, payload)
        raw_path = Path(result["raw_path"])
        self.assertTrue(result["raw_saved"])
        self.assertEqual(json.loads(raw_path.read_text()), payload)
        report = self.store.public()
        self.assertEqual(report["videos"][0]["selection_note"], video["selection_note"])
        self.assertEqual(report["videos"][0]["observed_at_basis"], video["observed_at_basis"])
        self.assertEqual(report["comments"][0]["emoji_images"], ["[捂脸]"])
        self.assertEqual(report["comments"][0]["text_note"], video["comments"][0]["text_note"])
        counts = report["counts"]
        self.assertTrue(self.store.ingest(rid, payload)["reused"])
        self.assertEqual(self.store.public()["counts"], counts)
        raw_path.unlink()  # Simulate a pre-archive-version batch in this temporary test directory.
        replay = self.store.ingest(rid, payload)
        self.assertTrue(replay["reused"])
        self.assertTrue(replay["raw_saved"])
        self.assertEqual(json.loads(raw_path.read_text()), payload)
        self.assertEqual(self.store.public()["counts"], counts)

    def test_raw_batch_ids_cannot_escape_the_run_directory(self):
        rid = self.request()
        for batch in ("../../../outside", "/absolute/path", "中文批次", "a"*500):
            result = self.store.ingest(rid, {"batch_id": batch, "videos": []})
            saved = Path(result["raw_path"])
            self.assertEqual(saved.parent, self.root / "data" / "raw" / rid)
            self.assertTrue(saved.is_file())
            self.assertNotIn("/", saved.name)

    def test_csv_retains_date_range_and_text_fidelity(self):
        rid = self.request()
        video = self.video()
        video["comments"][0].update(date=None, date_range={"start": "2026-08-18", "end": "2026-08-24", "basis": "可见3周前区间"}, emoji_images=["[捂脸]"], text_note="有一张表情图片")
        self.store.ingest(rid, {"videos": [video]})
        output = app.export_csv(self.store.public()).decode("utf-8-sig")
        self.assertIn("日期范围起点", output)
        self.assertIn("2026-08-18", output)
        self.assertIn("有一张表情图片", output)
        self.assertIn("[捂脸]", output)

    def test_concurrent_creation_has_only_one_active_request(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda n: app.Store(self.root).create("parallel%d" % n, queue=False), range(6)))
        self.assertEqual(len({r["request"]["id"] for r in results}), 1)
        self.assertEqual(sum(not r["reused"] for r in results), 1)

    def test_http_same_origin_and_real_result_endpoint(self):
        (self.root / "static").mkdir()
        (self.root / "static" / "index.html").write_text("<html>测试中文页面</html>")
        (self.root / "static" / "handoff.html").write_text("<html>测试回传</html>")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        proc = subprocess.Popen([sys.executable, str(Path(app.__file__)), "--root", str(self.root), "serve", "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = "http://127.0.0.1:" + str(port)
        try:
            for _ in range(40):
                try:
                    urllib.request.urlopen(base + "/api/state", timeout=1).close()
                    break
                except OSError:
                    time.sleep(.05)
            rid = self.request()
            body = json.dumps({"request_id": rid, "payload": {"videos": [self.video()], "batch_id": "browser-form"}}).encode()
            for origin in (None, "https://malicious.example"):
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                with self.assertRaises(urllib.error.HTTPError) as result:
                    urllib.request.urlopen(urllib.request.Request(base + "/api/result", data=body, headers=headers), timeout=2)
                self.assertEqual(result.exception.code, 403)
            request = urllib.request.Request(base + "/api/result", data=body, headers={"Content-Type": "application/json", "Origin": base})
            with urllib.request.urlopen(request, timeout=2) as result:
                self.assertEqual(result.status, 200)
            with urllib.request.urlopen(base + "/api/state", timeout=2) as result:
                self.assertEqual(json.load(result)["counts"]["new"], 1)
            with urllib.request.urlopen(base + "/api/history/" + rid + ".json", timeout=2) as result:
                self.assertEqual(json.load(result)["counts"]["new"], 1)
            with urllib.request.urlopen(base + "/handoff.html", timeout=2) as result:
                self.assertIn("回传", result.read().decode())
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
