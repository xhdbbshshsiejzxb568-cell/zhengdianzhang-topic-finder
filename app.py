#!/usr/bin/env python3
"""Local evidence ledger. Sampling is dispatched to an authorized Codex desktop task.

Python 3.9 standard library only. No platform network client or browser cookie access.
"""
import argparse
import copy
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote, parse_qs

ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
GROUPS = {"cq_douyin": "重庆 × 抖音", "cq_xhs": "重庆 × 小红书", "cn_douyin": "全国 × 抖音", "cn_xhs": "全国 × 小红书"}
ACTIVE = {"queued", "running"}
FINAL = {"completed", "partial", "blocked", "cancelled", "expired"}
LIMITATIONS = [
    "搜索和可见评论构成非随机样本；样本词频不是平台热榜、搜索量或全体购房者需求。",
    "只有实见播放器、播放控件或时长，且可读点赞≥1000或评论总数≥100的视频可以入选。",
    "每组至少3个入选视频、30条有近30天日期依据的有效独立评论才生成排名；单词至少有2个视频支持。",
    "搜索种子不是已验证的热门关键词；累计互动不能解释为近期增长。",
    "评论中的房价、学校、政策、产权和个人经历均是未核实说法，正式文案必须另行核验。",
    "采样需要这台Mac开机、Codex运行、Chrome及浏览器工具已授权；关机时不能本地采样。",
]


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def parse_time(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result
    except (ValueError, TypeError):
        return None


def reliable_date(value, basis):
    if not isinstance(basis, str) or not basis.strip() or re.search(r"编辑|edited|updated|modified|uncertain|inferred|estimated|unknown|missing.year|修改日期|猜测|推测|待核实|年份未显示|不确定|无年份|未显示年份", basis, re.I):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date().isoformat()
    except (ValueError, TypeError):
        return None


def is_recent(comment, reference):
    end = (parse_time(reference) or datetime.now(TZ)).astimezone(TZ).date()
    if comment.get("date_range") is not None:
        span = reliable_range(comment.get("date_range"), comment.get("date_basis"))
        if not span:
            return False
        start_day = datetime.strptime(span["start"], "%Y-%m-%d").date()
        end_day = datetime.strptime(span["end"], "%Y-%m-%d").date()
        return end - timedelta(days=30) <= start_day <= end_day <= end
    value = reliable_date(comment.get("date"), comment.get("date_basis"))
    if not value:
        return False
    day = datetime.strptime(value, "%Y-%m-%d").date()
    return end - timedelta(days=30) <= day <= end


def reliable_range(value, basis=None):
    if not isinstance(value, dict):
        return None
    basis = value.get("basis") or basis
    start = reliable_date(value.get("start"), basis)
    end = reliable_date(value.get("end"), basis)
    if not start or not end or start > end:
        return None
    return {"start": start, "end": end, "basis": basis}


def normalize_comment_id(platform, value):
    if value is None or str(value).strip() == "":
        return None
    value = str(value).strip()
    if platform == "xhs":
        match = re.fullmatch(r"(?:comment-)?([0-9a-fA-F]{24})", value)
        if match:
            return match.group(1).lower()
    return value


def canonical_url(url):
    try:
        part = urlparse(str(url))
        if part.scheme not in {"https", "http"} or not part.hostname:
            return None
        host = part.hostname.lower()
        if not any(host == x or host.endswith("." + x) for x in ("douyin.com", "xiaohongshu.com")):
            return None
        platform = "douyin" if host == "douyin.com" or host.endswith(".douyin.com") else "xhs"
        visible_id = visible_video_id(platform, url)
        if visible_id:
            return ("https://www.douyin.com/video/" if platform == "douyin" else "https://www.xiaohongshu.com/explore/") + visible_id
        return "https://" + host + part.path.rstrip("/")
    except (ValueError, TypeError):
        return None


def visible_video_id(platform, url):
    try:
        part = urlparse(str(url))
        host = (part.hostname or "").lower()
        if platform == "douyin" and (host == "douyin.com" or host.endswith(".douyin.com")):
            modal = parse_qs(part.query).get("modal_id", [None])[0]
            if modal and re.fullmatch(r"\d+", modal):
                return modal
            match = re.search(r"/video/(\d+)(?:/|$)", part.path)
            return match.group(1) if match else None
        if platform == "xhs" and (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com")):
            match = re.search(r"/(?:explore|search_result)/([0-9a-fA-F]{24})(?:/|$)", part.path)
            return match.group(1).lower() if match else None
    except (ValueError, TypeError):
        return None
    return None


def resolved_video_id(platform, video_id, url=None):
    given = str(video_id).strip() if video_id is not None and str(video_id).strip() else None
    if platform == "xhs" and given and re.fullmatch(r"[0-9a-fA-F]{24}", given):
        given = given.lower()
    visible = visible_video_id(platform, url)
    if visible and given and visible != given:
        raise ValueError("传入视频ID与公开URL中的视频ID不一致，拒绝合并")
    return visible or given


def video_key(platform, video_id, url=None):
    identity = resolved_video_id(platform, video_id, url) or canonical_url(url)
    if platform not in {"xhs", "douyin"} or not identity:
        raise ValueError("视频必须有平台及可见视频ID或规范化原帖链接")
    return platform + "|" + identity


def comment_key(platform, video_id, comment_id, url=None):
    cid = normalize_comment_id(platform, comment_id)
    if cid is None:
        return None
    return video_key(platform, video_id, url) + "|" + cid


def metric(value):
    if not isinstance(value, dict):
        return None
    number, raw = value.get("value"), value.get("raw")
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0 or not isinstance(raw, str) or not raw.strip():
        return None
    if re.search(r"未知|未显示|不明|unknown|unavailable", raw, re.I):
        return None
    matches = list(re.finditer(r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(万|亿|[kKmM])?", raw))
    if len(matches) != 1:
        return None
    shown = float(matches[0].group(1).replace(",", "")) * {None: 1, "万": 10000, "亿": 100000000, "k": 1000, "m": 1000000}.get((matches[0].group(2) or "").lower() or None, 1)
    if not math.isclose(number, shown, rel_tol=1e-9, abs_tol=1e-9):
        return None
    approximate = bool(value.get("approximate")) or bool(re.search(r"万|亿|[kKmM]|[+＋]|约", raw))
    return {"value": number, "raw": raw, "approximate": approximate}


def actual_video(evidence):
    if isinstance(evidence, dict):
        return evidence.get("type") in {"player", "controls", "duration"} and bool(evidence.get("detail"))
    return isinstance(evidence, str) and bool(re.search(r"播放器|播放控件|时长|player|controls|duration", evidence, re.I))


def exclusion(comment):
    if comment.get("excluded_reason"):
        return str(comment["excluded_reason"])
    for field, reason in (("is_author", "明确作者回复"), ("is_bot", "明确机器人内容"), ("is_platform_ai", "明确平台AI内容"), ("is_ad", "明确广告")):
        if comment.get(field) is True:
            return reason
    if not re.search(r"[\w\u3400-\u9fff]", str(comment.get("text", "")), re.UNICODE):
        return "纯表情或无有效文字"
    return None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def safe_batch_filename(batch_id):
    """Keep human-readable batch IDs when safe; hash all other public IDs."""
    text = str(batch_id)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text):
        return "batch-" + text + ".json"
    return "sha256-" + hashlib.sha256(text.encode("utf-8")).hexdigest() + ".json"


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.path = self.data / "state.json"
        self.lock = self.data / ".state.lock"
        self.thread_lock = threading.RLock()

    def config(self):
        path = self.root / "config.json"
        return json.loads(path.read_text("utf-8")) if path.exists() else {}

    @contextmanager
    def transaction(self):
        with self.thread_lock, self.lock.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                state = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {
                    "version": 1, "updated_at": now(), "requests": {}, "videos": {}, "comments": {}, "unknown_comments": {}, "semantics": {}, "latest_id": None,
                }
                yield state
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def save(self, state):
        state["updated_at"] = now()
        # The ledger is authoritative. Reports are a transactional outbox:
        # their exact snapshot is committed inside state before any report is published.
        atomic_json(self.path, state)
        for request in state["requests"].values():
            try:
                atomic_json(self.data / "requests" / (request["id"] + ".json"), request)
            except OSError as exc:
                sys.stderr.write("request mirror pending: " + str(exc) + "\n")
        flushed = []
        for rid, report in state.get("_pending_reports", {}).items():
            try:
                atomic_json(self.data / "reports" / (rid + ".json"), report)
                flushed.append(rid)
            except OSError as exc:
                sys.stderr.write("report mirror pending: " + str(exc) + "\n")
        if flushed:
            for rid in flushed:
                state["_pending_reports"].pop(rid, None)
            try:
                atomic_json(self.path, state)
            except OSError as exc:
                # The first committed state still contains exact outbox snapshots.
                sys.stderr.write("report outbox cleanup pending: " + str(exc) + "\n")

    def expire(self, state):
        changed = False
        for request in state["requests"].values():
            if request["status"] in ACTIVE and (parse_time(request["expires_at"]) or datetime.min.replace(tzinfo=TZ)) <= datetime.now(TZ):
                request.update(status="expired", reason="请求已超过90分钟有效期，停止后续采样；已保存部分结果。", finished_at=now())
                self.snapshot(state, request["id"])
                changed = True
        return changed

    def require_active(self, state, rid, running=False):
        if self.expire(state):
            self.save(state)
        request = state["requests"].get(rid)
        if not request:
            raise ValueError("请求不存在")
        allowed = {"running"} if running else ACTIVE
        if request["status"] not in allowed:
            raise ValueError("请求已取消、过期、完成或不能在当前状态执行：" + request["status"])
        return request

    def create(self, request_key=None, queue=True, schedule_date=None):
        with self.transaction() as state:
            self.expire(state)
            if request_key:
                found = next((r for r in state["requests"].values() if r.get("request_key") == request_key), None)
                if found:
                    self.save(state)
                    return {"request": copy.deepcopy(found), "reused": True}
            active = next((r for r in state["requests"].values() if r["status"] in ACTIVE), None)
            if active:
                self.save(state)
                return {"request": copy.deepcopy(active), "reused": True}
            config = self.config()
            target = config.get("target_thread")
            if not target:
                raise ValueError("config.json 尚未配置已核验的 Codex 目标任务")
            rid = str(uuid.uuid4())
            request = {
                "id": rid, "request_key": request_key or rid, "target_thread": target,
                "created_at": now(), "expires_at": (datetime.now(TZ) + timedelta(minutes=90)).isoformat(timespec="seconds"),
                "status": "queued", "reason": None, "date": datetime.now(TZ).date().isoformat(), "schedule_date": schedule_date,
                "progress": [{"at": now(), "text": "请求已保存，等待 Codex 浏览器采样。"}],
                "counts": {"read": 0, "new": 0, "duplicates": 0}, "seen_keys": [], "video_keys": [],
                "unknown_keys": [], "platform_status": {}, "validation_errors": [], "dispatch": "pending" if queue else "direct",
            }
            state["requests"][rid] = request
            state["latest_id"] = rid
            self.save(state)
        if queue:
            self.dispatch(rid)
        with self.transaction() as state:
            return {"request": copy.deepcopy(state["requests"][rid]), "reused": False}

    def dispatch(self, rid):
        config = self.config()
        with self.transaction() as state:
            request = self.require_active(state, rid)
            if request.get("dispatch") != "pending":
                return
            request["dispatch"] = "dispatching"
            self.save(state)
            target = request["target_thread"]
        message = (
            "执行郑店长房产选题助手请求 " + rid + "。先读取 " + str(self.root / "SAMPLER.md")
            + " 和 " + str(self.data / "requests" / (rid + ".json"))
            + "。使用 app.py claim 验证请求、目标任务和有效期后，才可使用当前任务已授权浏览器工具采样。"
            + "禁止普通CLI自行使用浏览器、抓包接口或账号凭据。每个视频后先保存部分结果，停止/过期则立即结束。"
        )
        try:
            result = subprocess.run([config.get("codex_path", "/Applications/ChatGPT.app/Contents/Resources/codex"), "queue", "--thread", target, "--message", message], capture_output=True, text=True, timeout=30)
            ok = result.returncode == 0
            error = (result.stderr or result.stdout or "Codex派单失败")[-1200:] if not ok else None
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, error = False, str(exc)
        with self.transaction() as state:
            request = state["requests"][rid]
            request["dispatch"] = "queued" if ok else "failed"
            if not ok and request["status"] in ACTIVE:
                request.update(status="blocked", reason="Codex派单失败：" + error, finished_at=now())
                self.snapshot(state, rid)
            self.save(state)

    def claim(self, rid, target):
        with self.transaction() as state:
            request = self.require_active(state, rid)
            if target != request["target_thread"]:
                raise ValueError("目标任务不匹配，禁止采样")
            if request["status"] != "queued":
                raise ValueError("请求已被领取，禁止重复采样")
            request.update(status="running", claimed_at=now(), claimed_by=target)
            request["progress"].append({"at": now(), "text": "Codex 已领取；正在检查平台可用性。"})
            self.save(state)
            return copy.deepcopy(request)

    def progress(self, rid, text):
        with self.transaction() as state:
            request = self.require_active(state, rid, running=True)
            request["progress"].append({"at": now(), "text": str(text)[:3000]})
            self.save(state)
            return copy.deepcopy(request)

    def ingest(self, rid, payload):
        if not isinstance(payload, dict):
            raise ValueError("结果必须是JSON对象")
        # Reject non-JSON numeric values before touching a transaction.
        json.dumps(payload, allow_nan=False)
        with self.transaction() as state:
            request = self.require_active(state, rid, running=True)
            batch_id = payload.get("batch_id")
            if batch_id is not None and (not isinstance(batch_id, str) or not batch_id):
                raise ValueError("batch_id必须是非空字符串或null")
            # Raw inputs are evidence archives, never a source for counting. Keeping an
            # input that later fails validation cannot publish it as accepted data.
            archive_id = batch_id or "unkeyed-" + str(uuid.uuid4())
            raw_path = self.data / "raw" / rid / safe_batch_filename(archive_id)
            raw_saved = not raw_path.exists()
            if raw_saved:
                atomic_json(raw_path, payload)
            batch_ids = request.setdefault("batch_ids", [])
            if batch_id and batch_id in batch_ids:
                return {"reused": True, "request": copy.deepcopy(request), "raw_path": str(raw_path), "raw_saved": raw_saved}
            errors = []
            for incoming in payload.get("videos", []):
                if not isinstance(incoming, dict):
                    errors.append("忽略非对象视频记录")
                    continue
                platform, group = incoming.get("platform"), incoming.get("group")
                if group not in GROUPS or group.split("_")[1] != platform:
                    errors.append("忽略平台与分组不一致视频")
                    continue
                try:
                    vkey = video_key(platform, incoming.get("video_id"), incoming.get("url"))
                    resolved_id = resolved_video_id(platform, incoming.get("video_id"), incoming.get("url"))
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                if not canonical_url(incoming.get("url")):
                    errors.append("忽略缺少有效原帖链接的视频")
                    continue
                previous = state["videos"].get(vkey, {})
                if previous.get("group") and previous["group"] != group:
                    errors.append("拒绝将同一视频跨组重复使用：%s 已归属 %s，本次 %s；请换其他视频。" % (vkey, previous["group"], group))
                    continue
                record = {k: incoming.get(k) for k in ("platform", "group", "video_id", "url", "title", "author", "date_raw", "published_date", "published_date_basis", "video_evidence", "search", "observed_at_basis", "selection_note", "observed_url", "video_id_basis", "author_basis", "source_note")}
                record["video_id"] = resolved_id
                record["canonical_url"] = canonical_url(incoming.get("url"))
                record["key"] = vkey
                record["observed_at"] = incoming.get("observed_at") if parse_time(incoming.get("observed_at")) else None
                record["ingested_at"] = now()
                record["likes"] = metric(incoming.get("likes"))
                record["comment_count"] = metric(incoming.get("comment_count"))
                record["published_date"] = reliable_date(record.get("published_date"), record.get("published_date_basis"))
                if not record["published_date"] and previous.get("published_date"):
                    for field in ("published_date", "published_date_basis", "date_raw"):
                        record[field] = previous.get(field)
                record["eligible"] = actual_video(record["video_evidence"]) and ((record["likes"] or {}).get("value", -1) >= 1000 or (record["comment_count"] or {}).get("value", -1) >= 100)
                record["eligibility_reason"] = "已观察视频证据且达到互动门槛" if record["eligible"] else "视频证据或可读互动未达到入选门槛"
                state["videos"][vkey] = record
                if vkey not in request["video_keys"]:
                    request["video_keys"].append(vkey)
                for raw in incoming.get("comments", []):
                    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str) or not raw["text"].strip():
                        errors.append("忽略没有原文的评论记录")
                        continue
                    request["counts"]["read"] += 1
                    cid = normalize_comment_id(platform, raw.get("comment_id"))
                    ckey = comment_key(platform, resolved_id, cid, incoming.get("url"))
                    record_c = {k: raw.get(k) for k in ("text", "author", "date_raw", "date", "date_basis", "date_range", "is_author", "is_bot", "is_platform_ai", "is_ad", "excluded_reason", "emoji_images", "text_note", "comment_id_basis", "observed_at_basis", "source_locator", "exclusion_basis")}
                    record_c.update(platform=platform, group=group, video_id=resolved_id, video_key=vkey, comment_id=cid, likes=metric(raw.get("likes")), observed_at=raw.get("observed_at") if parse_time(raw.get("observed_at")) else record["observed_at"], ingested_at=record["ingested_at"])
                    record_c["date"] = reliable_date(raw.get("date"), raw.get("date_basis"))
                    record_c["excluded_reason"] = exclusion(record_c)
                    if ckey is None:
                        unknown = rid + "|" + vkey + "|" + hashlib.sha256((raw["text"] + "|" + str(raw.get("author"))).encode()).hexdigest()[:24]
                        record_c.update(key=unknown, excluded_reason=record_c["excluded_reason"] or "缺少可见评论ID，无法可靠跨次去重", first_seen=record["ingested_at"], last_seen=record["ingested_at"])
                        state["unknown_comments"][unknown] = record_c
                        if unknown not in request["unknown_keys"]:
                            request["unknown_keys"].append(unknown)
                        continue
                    previous_c = state["comments"].get(ckey)
                    if previous_c:
                        request["counts"]["duplicates"] += 1
                        if not record_c["date"] and reliable_date(previous_c.get("date"), previous_c.get("date_basis")):
                            for field in ("date", "date_basis", "date_raw"):
                                record_c[field] = previous_c.get(field)
                        if record_c.get("date_range") is None and reliable_range(previous_c.get("date_range"), previous_c.get("date_basis")):
                            record_c["date_range"] = previous_c["date_range"]
                        for field in ("is_author", "is_bot", "is_platform_ai", "is_ad"):
                            if record_c.get(field) is None:
                                record_c[field] = previous_c.get(field)
                        if not record_c["excluded_reason"] and previous_c.get("excluded_reason"):
                            record_c["excluded_reason"] = previous_c["excluded_reason"]
                    else:
                        request["counts"]["new"] += 1
                    record_c.update(key=ckey, first_seen=(previous_c or {}).get("first_seen", record["ingested_at"]), last_seen=record["ingested_at"])
                    state["comments"][ckey] = record_c
                    if ckey not in request["seen_keys"]:
                        request["seen_keys"].append(ckey)
            # Membership only comes from explicit ID + exact-original-quote pairs.
            semantics = state["semantics"].setdefault(rid, {})
            for proposed in payload.get("semantic_groups", []):
                if not isinstance(proposed, dict) or proposed.get("group") not in GROUPS or not proposed.get("label"):
                    errors.append("忽略缺少分组或词名的语义词组")
                    continue
                refs, quotes = [], {}
                for reference in proposed.get("comment_refs", []):
                    try:
                        ckey = comment_key(reference.get("platform"), reference.get("video_id"), reference.get("comment_id"), reference.get("url"))
                    except (ValueError, AttributeError):
                        ckey = None
                    comment = state["comments"].get(ckey)
                    if not comment or ckey not in request["seen_keys"] or reference.get("quote") != comment.get("text") or comment.get("group") != proposed["group"] or comment.get("excluded_reason"):
                        errors.append("拒绝不存在、非本轮、原文不匹配、跨组或被排除的评论引用：" + str(ckey))
                        continue
                    if ckey not in refs:
                        refs.append(ckey)
                        quotes[ckey] = comment["text"]
                if not refs:
                    errors.append("词组没有可验证原评论：" + str(proposed["label"]))
                    continue
                topic = proposed.get("topic")
                if topic and (not isinstance(topic, dict) or not all(topic.get(k) for k in ("title", "angle", "outline"))):
                    errors.append("选题缺少标题、角度或提纲：" + str(proposed["label"]))
                    topic = None
                topic_keys = None
                if topic and "comment_refs" in topic:
                    topic_keys = []
                    selected = topic.get("comment_refs")
                    if not isinstance(selected, list):
                        errors.append("选题comment_refs必须是原评论引用列表：" + str(proposed["label"]))
                        selected = []
                    for reference in selected:
                        try:
                            ckey = comment_key(reference.get("platform"), reference.get("video_id"), reference.get("comment_id"), reference.get("url"))
                        except (ValueError, AttributeError):
                            ckey = None
                        if ckey not in refs or not isinstance(reference, dict) or reference.get("quote") != quotes.get(ckey):
                            errors.append("拒绝不属于该词组或原文不匹配的选题引用：" + str(ckey))
                            continue
                        if ckey not in topic_keys:
                            topic_keys.append(ckey)
                    if not topic_keys:
                        errors.append("选题没有通过校验的独立引用，不生成选题：" + str(proposed["label"]))
                        topic = None
                semantics[proposed["group"] + "|" + str(proposed["label"])] = {
                    "group": proposed["group"], "label": str(proposed["label"]), "keys": refs, "quotes": quotes, "topic": topic, "topic_keys": topic_keys,
                }
            if isinstance(payload.get("platform_status"), dict):
                request["platform_status"].update(payload["platform_status"])
            request["validation_errors"].extend(errors)
            if batch_id:
                batch_ids.append(batch_id)
            request["progress"].append({"at": now(), "text": "部分结果已原子保存：本轮读取%d条，首次新增%d条，复查重复%d条。" % (request["counts"]["read"], request["counts"]["new"], request["counts"]["duplicates"])})
            self.snapshot(state, rid)
            self.save(state)
            return {"reused": False, "request": copy.deepcopy(request), "validation_errors": errors, "raw_path": str(raw_path), "raw_saved": raw_saved}

    def finish(self, rid, status, reason=None):
        if status not in {"completed", "partial", "blocked"}:
            raise ValueError("非法结束状态")
        with self.transaction() as state:
            request = self.require_active(state, rid)
            request.update(status=status, reason=reason, finished_at=now())
            request["progress"].append({"at": now(), "text": reason or "本轮已结束，日报与历史已保存。"})
            self.snapshot(state, rid)
            self.save(state)
            return copy.deepcopy(request)

    def stop(self, rid=None):
        with self.transaction() as state:
            self.expire(state)
            request = state["requests"].get(rid) if rid else next((r for r in state["requests"].values() if r["status"] in ACTIVE), None)
            if request and request["status"] in ACTIVE:
                request.update(status="cancelled", reason="用户停止任务；已保存部分结果。", finished_at=now())
                request["progress"].append({"at": now(), "text": request["reason"]})
                self.snapshot(state, request["id"])
            self.save(state)
            return {"request": copy.deepcopy(request)}

    def evidence(self, state, comment, reference):
        item = copy.deepcopy(comment)
        video = state["videos"].get(comment["video_key"], {})
        item.update(video_url=video.get("url"), video_title=video.get("title"), recent=is_recent(comment, reference))
        return item

    def report(self, state, rid=None):
        request = state["requests"].get(rid or state.get("latest_id"))
        reference = request.get("finished_at") or now() if request else now()
        comments = [self.evidence(state, state["comments"][k], reference) for k in (request or {}).get("seen_keys", []) if k in state["comments"]]
        unknown = [self.evidence(state, state["unknown_comments"][k], reference) for k in (request or {}).get("unknown_keys", []) if k in state["unknown_comments"]]
        valid = [c for c in comments if not c["excluded_reason"] and state["videos"].get(c["video_key"], {}).get("eligible")]
        recent = [c for c in valid if c["recent"]]
        video_list = []
        for vkey in (request or {}).get("video_keys", []):
            if vkey in state["videos"]:
                item = copy.deepcopy(state["videos"][vkey])
                item["comments"] = [c for c in comments + unknown if c["video_key"] == vkey]
                video_list.append(item)
        group_list, keywords, topics = [], [], []
        for gid, label in GROUPS.items():
            vids = [v for v in video_list if v["group"] == gid and v.get("eligible")]
            good = [c for c in valid if c["group"] == gid]
            fresh = [c for c in recent if c["group"] == gid]
            qualified = len(vids) >= 3 and len(fresh) >= 30
            group_keywords, group_topics = [], []
            for semantic in state["semantics"].get((request or {}).get("id"), {}).values():
                if semantic["group"] != gid:
                    continue
                refs = [c for c in good if c["key"] in semantic["keys"] and semantic.get("quotes", {}).get(c["key"], c["text"]) == c["text"]]
                current_refs = [c for c in refs if c["recent"]]
                fresh_vids = {c["video_key"] for c in current_refs}
                ranked = qualified and len(fresh_vids) >= 2
                used = current_refs if ranked else refs
                if not used:
                    continue
                explicit = [{k: c.get(k) for k in ("key", "platform", "video_id", "comment_id", "video_url", "video_title", "likes", "date", "date_raw", "date_basis", "date_range", "recent", "emoji_images", "text_note", "comment_id_basis")} for c in used]
                for pair, c in zip(explicit, used):
                    pair["quote"] = c["text"]
                word = {"group": gid, "label": semantic["label"], "status": "ranked" if ranked else "exploratory", "comment_count": len(used), "video_count": len({c["video_key"] for c in used}), "recent_comment_count": len(current_refs), "recent_video_count": len(fresh_vids), "like_sum": sum((c.get("likes") or {}).get("value", 0) for c in used) if any(c.get("likes") for c in used) else None, "like_sum_approximate": any((c.get("likes") or {}).get("approximate") for c in used), "comment_refs": explicit}
                group_keywords.append(word)
                if semantic.get("topic"):
                    selected_keys = semantic.get("topic_keys")
                    topic_comments = used if selected_keys is None else [c for c in refs if c["key"] in selected_keys]
                    if not topic_comments:
                        continue
                    selected_refs = [{k: c.get(k) for k in ("key", "platform", "video_id", "comment_id", "video_url", "video_title", "likes", "date", "date_raw", "date_basis", "date_range", "recent", "emoji_images", "text_note", "comment_id_basis")} for c in topic_comments]
                    for pair, c in zip(selected_refs, topic_comments):
                        pair["quote"] = c["text"]
                    topic = {k: semantic["topic"].get(k) for k in ("title", "angle", "outline")}
                    topic_status = word["status"] if all(c["recent"] for c in topic_comments) else "exploratory"
                    topic.update(group=gid, keyword=semantic["label"], status=topic_status, comment_refs=selected_refs, fact_status="评论说法未经核实；发布前核验房价、学校、政策、产权及个人经历。")
                    group_topics.append(topic)
            group_keywords.sort(key=lambda x: (x["status"] != "ranked", -x["comment_count"], -x["video_count"], x["label"]))
            group_list.append({"id": gid, "label": label, "status": "qualified" if qualified else "insufficient", "reason": "达到组内采样门槛" if qualified else "入选视频%d/3，近期有效评论%d/30；仅展示探索线索。" % (len(vids), len(fresh)), "videos": len(vids), "valid_comments": len(good), "recent_comments": len(fresh), "keywords": group_keywords, "topics": group_topics})
            keywords.extend(group_keywords)
            topics.extend(group_topics)
        counts = dict((request or {}).get("counts", {"read": 0, "new": 0, "duplicates": 0}))
        counts.update(videos=sum(v.get("eligible", False) for v in video_list), observed_videos=len(video_list), independent_comments=len(state["comments"]), sampled_independent_comments=len(comments), unknown_id_comments=len(unknown), valid_comments=len(valid), recent_comments=len(recent), ranked_keywords=sum(x["status"] == "ranked" for x in keywords), exploratory_keywords=sum(x["status"] == "exploratory" for x in keywords), topics=len(topics))
        return {"app_name": "郑店长房产选题助手", "id": (request or {}).get("id"), "date": (request or {}).get("date"), "created_at": (request or {}).get("created_at"), "updated_at": now(), "status": (request or {}).get("status", "idle"), "reason": (request or {}).get("reason"), "request": copy.deepcopy(request), "counts": counts, "groups": group_list, "videos": video_list, "comments": comments + unknown, "keywords": keywords, "topics": topics, "platform_status": copy.deepcopy((request or {}).get("platform_status", {})), "validation_errors": copy.deepcopy((request or {}).get("validation_errors", [])), "limitations": LIMITATIONS}

    def snapshot(self, state, rid):
        report = self.report(state, rid)
        state.setdefault("_pending_reports", {})[rid] = report

    def public(self):
        with self.transaction() as state:
            if self.expire(state):
                self.save(state)
            report = self.report(state)
            report["updated_at"] = state["updated_at"]
            report["active_request"] = copy.deepcopy(next((r for r in state["requests"].values() if r["status"] in ACTIVE), None))
            report["requests"] = sorted(copy.deepcopy(list(state["requests"].values())), key=lambda r: r["created_at"], reverse=True)
            report["history"] = []
            for r in report["requests"]:
                path = self.data / "reports" / (r["id"] + ".json")
                saved = state.get("_pending_reports", {}).get(r["id"])
                if saved is None and path.exists():
                    saved = json.loads(path.read_text("utf-8"))
                if saved is not None:
                    report["history"].append({k: saved.get(k) for k in ("id", "date", "created_at", "status", "reason", "counts")})
            return report

    def history(self, rid):
        if not re.fullmatch(r"[0-9a-f-]{36}", rid):
            raise ValueError("无效日报ID")
        with self.transaction() as state:
            pending = state.get("_pending_reports", {}).get(rid)
            if pending is not None:
                return copy.deepcopy(pending)
            path = self.data / "reports" / (rid + ".json")
            if not path.exists():
                raise ValueError("日报不存在")
            return json.loads(path.read_text("utf-8"))


def export_csv(report):
    stream = io.StringIO()
    stream.write("\ufeff")
    writer = csv.writer(stream)
    writer.writerow(["分组", "平台", "视频ID", "视频标题", "原帖链接", "评论ID", "原评论", "可见点赞", "原始日期", "日期", "日期依据", "日期范围起点", "日期范围终点", "日期范围依据", "近期依据", "排除原因", "观察时间", "入库时间", "文字保真说明", "表情图片记录", "评论ID依据"])
    for c in report["comments"]:
        span = c.get("date_range") if isinstance(c.get("date_range"), dict) else {}
        emoji = json.dumps(c["emoji_images"], ensure_ascii=False) if c.get("emoji_images") is not None else None
        row = [GROUPS.get(c.get("group")), c.get("platform"), c.get("video_id"), c.get("video_title"), c.get("video_url"), c.get("comment_id"), c.get("text"), (c.get("likes") or {}).get("raw"), c.get("date_raw"), c.get("date"), c.get("date_basis"), span.get("start"), span.get("end"), span.get("basis") or c.get("date_basis"), c.get("recent"), c.get("excluded_reason"), c.get("observed_at"), c.get("ingested_at"), c.get("text_note"), emoji, c.get("comment_id_basis")]
        # Spreadsheet exports must not turn untrusted comments into formulas.
        writer.writerow(["'" + str(v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in row])
    return stream.getvalue().encode("utf-8")


def serve(store, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (now(), fmt % args))

        def allowed(self, mutate=False):
            expected = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
            host = self.headers.get("Host", "")
            if host not in expected:
                return False
            origin = self.headers.get("Origin")
            if mutate:
                return origin == "http://" + host
            return origin is None or origin == "http://" + host

        def send(self, payload, status=200, content_type="application/json; charset=utf-8", attachment=None):
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if not isinstance(payload, bytes) else payload
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
            if attachment:
                self.send_header("Content-Disposition", 'attachment; filename="' + attachment + '"')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.allowed():
                return self.send({"error": "仅允许本机同源访问"}, 403)
            path = unquote(urlparse(self.path).path)
            try:
                if path in {"/api/state", "/api/export.json"}:
                    return self.send(store.public(), attachment="topic-assistant.json" if path.endswith("export.json") else None)
                if path == "/api/export.csv":
                    return self.send(export_csv(store.public()), content_type="text/csv; charset=utf-8", attachment="comment-evidence.csv")
                if path.startswith("/api/history/") and path.endswith(".json"):
                    return self.send(store.history(path.split("/")[-1][:-5]))
                if path in {"/", "/index.html", "/handoff.html", "/static/handoff.html"}:
                    html = store.root / "static" / ("handoff.html" if path.endswith("handoff.html") else "index.html")
                    return self.send(html.read_bytes(), content_type="text/html; charset=utf-8")
                return self.send({"error": "不存在"}, 404)
            except (ValueError, OSError) as exc:
                return self.send({"error": str(exc)}, 404)

        def do_POST(self):
            if not self.allowed(mutate=True):
                return self.send({"error": "状态更改必须来自本机页面且Origin同源"}, 403)
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send({"error": "需要JSON请求"}, 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size > (8 * 1024 * 1024 if urlparse(self.path).path == "/api/result" else 65536) or size < 0:
                    return self.send({"error": "请求过大"}, 413)
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("请求必须是对象")
                path = urlparse(self.path).path
                if path == "/api/sample":
                    key = body.get("request_key")
                    if key is not None and (not isinstance(key, str) or len(key) > 200):
                        raise ValueError("无效请求幂等键")
                    return self.send(store.create(key))
                if path == "/api/stop":
                    return self.send(store.stop(body.get("request_id")))
                if path == "/api/result":
                    return self.send(store.ingest(body.get("request_id"), body.get("payload")))
                return self.send({"error": "不存在"}, 404)
            except (ValueError, TypeError, OSError) as exc:
                return self.send({"error": str(exc)}, 400)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True

    def expiry_loop():
        while True:
            threading.Event().wait(10)
            try:
                store.public()
            except Exception as exc:
                sys.stderr.write("expiry check failed: " + str(exc) + "\n")

    threading.Thread(target=expiry_loop, daemon=True).start()
    print("郑店长房产选题助手：http://127.0.0.1:%d" % port, flush=True)
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="郑店长房产选题助手本地证据账本")
    parser.add_argument("--root", default=str(ROOT))
    subs = parser.add_subparsers(dest="command", required=True)
    web = subs.add_parser("serve")
    web.add_argument("--port", type=int, default=18765)
    claim = subs.add_parser("claim")
    claim.add_argument("id")
    claim.add_argument("--thread", required=True)
    ingest = subs.add_parser("ingest")
    ingest.add_argument("id")
    ingest.add_argument("file")
    finish = subs.add_parser("finish")
    finish.add_argument("id")
    finish.add_argument("--status", required=True, choices=["completed", "partial", "blocked"])
    finish.add_argument("--reason")
    progress = subs.add_parser("progress")
    progress.add_argument("id")
    progress.add_argument("text")
    scheduled = subs.add_parser("schedule-request")
    scheduled.add_argument("--date", required=True)
    stop = subs.add_parser("stop")
    stop.add_argument("id", nargs="?")
    subs.add_parser("state")
    args = parser.parse_args()
    store = Store(args.root)
    try:
        if args.command == "serve":
            serve(store, args.port)
            return
        if args.command == "claim":
            result = store.claim(args.id, args.thread)
        elif args.command == "ingest":
            result = store.ingest(args.id, json.loads(Path(args.file).read_text("utf-8")))
        elif args.command == "finish":
            result = store.finish(args.id, args.status, args.reason)
        elif args.command == "progress":
            result = store.progress(args.id, args.text)
        elif args.command == "schedule-request":
            day = datetime.strptime(args.date, "%Y-%m-%d").date().isoformat()
            if day != datetime.now(TZ).date().isoformat():
                raise ValueError("只能创建北京时间当天的定时请求")
            result = store.create("schedule:" + day, queue=False, schedule_date=day)
        elif args.command == "stop":
            result = store.stop(args.id)
        else:
            result = store.public()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
