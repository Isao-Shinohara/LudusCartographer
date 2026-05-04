"""タグ機能の Gemini 判定 (Phase 3 シーン / Phase 4 詳細)。

設計書: docs/design/master_node_tags.md §4.3 / §8
詳細計画: docs/design/master_node_tags_phase1.md §11 (P3 スコープ)
CLAUDE.md §17 / §21 ルール 3

CLI:
    python -m tools.tag_judgment --type scene --mode unassigned [--reset-manual]

仕様:
- 対象: マスターノードの代表 (lc_master_nodes) のみ
- キャッシュ: lc_tag_judgments の (master_fp, tag_type, prompt_hash, model)
- prompt_hash: プロンプト本文 + 候補タグ (id/name/description) の sha256
- エラーはキャッシュしない (再実行で復旧可能に、§17)
- 並列化: ThreadPoolExecutor 5 並列 (§17 と統一)
- API 使用量: record_api_usage(purpose='tag_scene_judgment' / 'tag_subscene_judgment')
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

_CRAWLER_ROOT = Path(__file__).parent.parent
if str(_CRAWLER_ROOT) not in sys.path:
    sys.path.insert(0, str(_CRAWLER_ROOT))

logger = logging.getLogger(__name__)

# ─── デフォルトプロンプト ─────────────────────────────

DEFAULT_PROMPTS: dict[str, str] = {
    "scene": """あなたはモバイルゲーム「マギアレコード Exedra」の画面分類器です。
以下の画面情報を解析し、最も該当するシーンタグを **必ず 1 つ** 選んでください。

# 候補タグ
{tag_candidates}

# 画面情報
- 検出器が推定したシーン: {detected_scene}
- 画面の OCR テキスト:
{ocr_text}

# 判定ガイドライン
- 候補から **必ず 1 つだけ** 選ぶこと (シーンタグは画面に 1 つしか付与されない)
- 検出器の推定はヒントだが、誤りの可能性もあるため OCR テキストも合わせて判断する
- 迷った場合は最も画面の主目的を表すタグを選ぶ

# 出力形式
以下の JSON 形式で出力してください。説明文や Markdown は付けないでください。
{{"tag_id": <選んだタグの id>, "confidence": <0.0〜1.0 の確信度>, "reasoning": "<判定理由 (50字以内)>"}}
""",
    "sub_scene": """あなたはモバイルゲーム「マギアレコード Exedra」の画面の詳細属性分類器です。
以下の画面情報を解析し、該当する詳細タグを **0 個以上** 全て選んでください。

# 候補タグ
{tag_candidates}

# 画面情報
- 検出器が推定したシーン: {detected_scene}
- 画面の OCR テキスト:
{ocr_text}

# 判定ガイドライン
- 候補から該当するもの **全て** を返す。該当なしなら空配列でよい
- 詳細タグはシーンに依存しない (例: 「ダイアログ」はバトル中でもホーム画面でも該当する)
- 確信度が低い場合は付与しない (= 偽陽性より偽陰性を許容)

# 出力形式
以下の JSON 形式で出力してください。説明文や Markdown は付けないでください。
{{"tag_ids": [<該当する全タグの id>], "confidence": <0.0〜1.0 の全体確信度>, "reasoning": "<判定理由 (100字以内)>"}}
""",
}

# モデル選択 (CLAUDE.md §17 と整合)
MODEL_BY_TYPE: dict[str, str] = {
    "scene": "gemini-2.5-flash-lite",
    "sub_scene": "gemini-2.5-flash",
}

# プロンプト用 purpose 文字列 (Cost タブ統合)
PURPOSE_BY_TYPE: dict[str, str] = {
    "scene": "tag_scene_judgment",
    "sub_scene": "tag_subscene_judgment",
}


# ─── prompt_hash 計算 ─────────────────────────────────


def compute_prompt_hash(prompt_text: str, candidate_tags: list[dict]) -> str:
    """プロンプト本文 + 候補タグの (id, name, description) を含む sha256 (16 文字)。

    description / プロンプト本文の変更でハッシュが変わり、自動再判定が走る。
    color / sort_order の変更ではハッシュ不変 (= キャッシュ維持)。
    """
    payload = {
        "prompt": prompt_text,
        "tags": [
            {
                "id": int(t["id"]),
                "name": t.get("name") or "",
                "description": t.get("description") or "",
            }
            for t in sorted(candidate_tags, key=lambda x: int(x["id"]))
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


# ─── プロンプト展開 ─────────────────────────────────


def render_tag_candidates(tags: list[dict]) -> str:
    """{tag_candidates} プレースホルダの展開。"""
    return "\n".join(
        f"- id={t['id']}, name=\"{t.get('name', '')}\","
        f" description=\"{t.get('description', '')}\""
        for t in tags
    )


def render_prompt(template: str, tags: list[dict],
                  detected_scene: str, ocr_text: str) -> str:
    """プロンプトテンプレートにプレースホルダを差し込む。"""
    return template.format(
        tag_candidates=render_tag_candidates(tags),
        detected_scene=detected_scene or "(不明)",
        ocr_text=ocr_text or "(なし)",
    )


# ─── Gemini REST API 呼び出し ─────────────────────────


_GEMINI_TIMEOUT = 60
_GEMINI_JSON_RETRIES = 2


def call_gemini(model: str, prompt_text: str, api_key: str) -> tuple[Optional[dict], int, int, Optional[str]]:
    """Gemini を呼び出してパース済みレスポンスを返す。

    Returns:
        (parsed_json, input_tokens, output_tokens, error_message)
        成功時: (dict, in, out, None)
        失敗時: (None, 0, 0, "<エラー説明>")
    """
    import urllib.request

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": 1024,
            "temperature": 0.1,
        },
    }).encode()

    last_err: Optional[Exception] = None
    for attempt in range(_GEMINI_JSON_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=_GEMINI_TIMEOUT) as resp:
                raw = resp.read()
            resp_data = json.loads(raw)
            usage = resp_data.get("usageMetadata", {})
            in_tok = int(usage.get("promptTokenCount", 0))
            out_tok = int(usage.get("candidatesTokenCount", 0))
            text_part = (
                resp_data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            try:
                parsed = json.loads(text_part)
                return parsed, in_tok, out_tok, None
            except json.JSONDecodeError as e:
                last_err = e
                if attempt < _GEMINI_JSON_RETRIES:
                    time.sleep(0.3 + attempt * 0.3)
                    continue
                return None, in_tok, out_tok, f"JSON parse: {e}"
        except Exception as e:
            last_err = e
            if attempt < _GEMINI_JSON_RETRIES:
                time.sleep(0.5 + attempt * 0.3)
                continue
            return None, 0, 0, f"{type(e).__name__}: {e}"

    return None, 0, 0, f"unreachable: {last_err}"


# ─── 判定ロジック ─────────────────────────────────────


def fetch_active_prompt(conn: sqlite3.Connection, tag_type: str) -> str:
    """lc_tag_prompts から現在のプロンプトを取得 (なければデフォルト挿入)。"""
    row = conn.execute(
        "SELECT prompt_text FROM lc_tag_prompts WHERE tag_type = ?",
        (tag_type,),
    ).fetchone()
    if row:
        return row[0] if not isinstance(row, sqlite3.Row) else row["prompt_text"]
    # 初回: デフォルトを挿入
    default_text = DEFAULT_PROMPTS[tag_type]
    conn.execute(
        "INSERT INTO lc_tag_prompts (tag_type, prompt_text, is_default)"
        " VALUES (?, ?, 1)",
        (tag_type, default_text),
    )
    conn.commit()
    return default_text


def fetch_candidate_tags(conn: sqlite3.Connection, tag_type: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, name, description FROM lc_tags"
        " WHERE tag_type = ? AND is_deleted = 0"
        " ORDER BY sort_order, id",
        (tag_type,),
    ).fetchall()
    return [dict(r) if isinstance(r, sqlite3.Row) else
            {"id": r[0], "name": r[1], "description": r[2]} for r in rows]


def fetch_target_master_fps(
    conn: sqlite3.Connection, tag_type: str, mode: str, reset_manual: bool,
    version_id: int,
) -> list[dict]:
    """対象 master_fp 一覧を返す (representative_screen_id + ocr_text + scene を join)。

    mode='unassigned': 「未付与のみ」 — assigned_by='auto_pilot'/'manual' で
        当該 tag_type が付与済みのものは除外 (Gemini 付与のみは prompt_hash 変化で再判定)
    mode='all': 全件再判定対象 — auto_pilot は常時保護、manual は reset_manual 時のみ上書き
    """
    sql = (
        "SELECT m.master_fp,"
        "  COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') AS ocr,"
        "  m.scene AS detected_scene,"
        "  m.title"
        " FROM lc_master_nodes m"
        " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
        " WHERE m.version_id = ?"
    )
    params: list = [version_id]

    if mode == "unassigned":
        # auto_pilot / manual のいずれかで当該 tag_type のタグが付いていれば除外
        sql += (
            " AND NOT EXISTS ("
            "   SELECT 1 FROM lc_master_node_tags mnt"
            "   JOIN lc_tags t ON t.id = mnt.tag_id"
            "   WHERE mnt.master_fp = m.master_fp AND mnt.version_id = ?"
            "     AND t.tag_type = ?"
            "     AND mnt.assigned_by IN ('auto_pilot', 'manual')"
            " )"
        )
        params.extend([version_id, tag_type])
    elif mode == "all":
        # auto_pilot は常に保護
        sql += (
            " AND NOT EXISTS ("
            "   SELECT 1 FROM lc_master_node_tags mnt"
            "   JOIN lc_tags t ON t.id = mnt.tag_id"
            "   WHERE mnt.master_fp = m.master_fp AND mnt.version_id = ?"
            "     AND t.tag_type = ?"
            "     AND mnt.assigned_by = 'auto_pilot'"
            " )"
        )
        params.extend([version_id, tag_type])
        # manual はデフォルトで保護
        if not reset_manual:
            sql += (
                " AND NOT EXISTS ("
                "   SELECT 1 FROM lc_master_node_tags mnt"
                "   JOIN lc_tags t ON t.id = mnt.tag_id"
                "   WHERE mnt.master_fp = m.master_fp AND mnt.version_id = ?"
                "     AND t.tag_type = ?"
                "     AND mnt.assigned_by = 'manual'"
                " )"
            )
            params.extend([version_id, tag_type])

    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        if isinstance(r, sqlite3.Row):
            out.append({
                "master_fp": r["master_fp"], "ocr": r["ocr"],
                "detected_scene": r["detected_scene"] or "",
                "title": r["title"] or "",
            })
        else:
            out.append({
                "master_fp": r[0], "ocr": r[1] or "",
                "detected_scene": r[2] or "", "title": r[3] or "",
            })
    return out


def get_cached_judgment(
    conn: sqlite3.Connection, master_fp: str, tag_type: str,
    prompt_hash: str, model: str,
) -> Optional[dict]:
    row = conn.execute(
        "SELECT result_json FROM lc_tag_judgments"
        " WHERE master_fp = ? AND tag_type = ? AND prompt_hash = ? AND model = ?",
        (master_fp, tag_type, prompt_hash, model),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0] if not isinstance(row, sqlite3.Row) else row["result_json"])
    except (json.JSONDecodeError, TypeError):
        return None


def save_cache(
    conn: sqlite3.Connection, master_fp: str, tag_type: str,
    prompt_hash: str, model: str, result: dict,
) -> None:
    """成功した判定結果のみキャッシュする (エラーは保存しない、§17)。"""
    conn.execute(
        "INSERT OR REPLACE INTO lc_tag_judgments"
        " (master_fp, tag_type, prompt_hash, result_json, model, judged_at)"
        " VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (master_fp, tag_type, prompt_hash, json.dumps(result, ensure_ascii=False), model),
    )


def apply_judgment(
    conn: sqlite3.Connection, master_fp: str, version_id: int, tag_type: str,
    result: dict, valid_tag_ids: set[int], reset_manual: bool,
) -> None:
    """判定結果を lc_master_node_tags に書き込む。

    - シーン (1 個必須): 既存 gemini シーンタグを削除 + 新規付与
    - 詳細 (0+): 既存 gemini 詳細タグを全削除 + 新規一括付与
    - reset_manual=True なら manual も一緒に削除
    """
    if tag_type == "scene":
        new_ids = []
        if "tag_id" in result and result["tag_id"] is not None:
            new_ids = [int(result["tag_id"])]
        elif "tag_ids" in result and isinstance(result["tag_ids"], list):
            new_ids = [int(x) for x in result["tag_ids"][:1]]
    else:
        new_ids = [int(x) for x in result.get("tag_ids", []) if isinstance(x, (int, float, str))]

    new_ids = [tid for tid in new_ids if tid in valid_tag_ids]
    confidence = float(result.get("confidence") or 1.0)

    # 既存 gemini を削除 (+ reset_manual なら manual も)
    delete_sources = ["gemini"] + (["manual"] if reset_manual else [])
    placeholders = ",".join(["?"] * len(delete_sources))
    conn.execute(
        f"DELETE FROM lc_master_node_tags"
        f" WHERE id IN ("
        f"   SELECT mnt.id FROM lc_master_node_tags mnt"
        f"   JOIN lc_tags t ON t.id = mnt.tag_id"
        f"   WHERE mnt.master_fp = ? AND mnt.version_id = ?"
        f"     AND t.tag_type = ?"
        f"     AND mnt.assigned_by IN ({placeholders})"
        f" )",
        (master_fp, version_id, tag_type, *delete_sources),
    )

    for tid in new_ids:
        conn.execute(
            "INSERT OR IGNORE INTO lc_master_node_tags"
            " (master_fp, version_id, tag_id, assigned_by, confidence, assigned_at)"
            " VALUES (?, ?, ?, 'gemini', ?, datetime('now'))",
            (master_fp, version_id, tid, confidence),
        )


# ─── オーケストレーター ────────────────────────────────


def write_progress(state_db_path: Path, payload: dict) -> None:
    """auto_pilot_state テーブルに tagging_progress を書き込む。"""
    try:
        conn = sqlite3.connect(str(state_db_path), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auto_pilot_state (
                key TEXT PRIMARY KEY, value TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO auto_pilot_state (key, value, updated_at)"
            " VALUES ('tagging_progress', ?, datetime('now'))",
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[TAG_JUDGE] progress write failed: %s", e)


def run_judgment(
    db_path: Path, tag_type: str, mode: str, reset_manual: bool,
    version_id: int, dry_run: bool = False, max_workers: int = 5,
) -> dict:
    """判定パイプラインを実行する (CLI / 単体呼び出しの両用)。"""
    from tools.ap.api_usage import record_api_usage

    api_key = os.environ.get("GEMINI_API_KEY", "")
    model = MODEL_BY_TYPE[tag_type]
    purpose = PURPOSE_BY_TYPE[tag_type]

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")

    write_progress(db_path, {
        "running": True, "phase": "preparing",
        "tag_type": tag_type, "mode": mode,
        "total": 0, "processed": 0, "cache_hits": 0,
        "api_calls": 0, "errors": 0,
        "started_at": time.time(),
    })

    candidate_tags = fetch_candidate_tags(conn, tag_type)
    valid_tag_ids = {int(t["id"]) for t in candidate_tags}
    if not candidate_tags:
        result = {
            "ok": False, "error": "no_candidate_tags",
            "message": f"候補タグが 0 件です (tag_type={tag_type})",
        }
        write_progress(db_path, {**result, "running": False, "phase": "error"})
        conn.close()
        return result

    prompt_template = fetch_active_prompt(conn, tag_type)
    prompt_hash = compute_prompt_hash(prompt_template, candidate_tags)

    targets = fetch_target_master_fps(conn, tag_type, mode, reset_manual, version_id)
    total = len(targets)

    counters = {"processed": 0, "cache_hits": 0, "api_calls": 0, "errors": 0}
    started = time.time()

    def _flush_progress(phase="judging"):
        write_progress(db_path, {
            "running": True, "phase": phase,
            "tag_type": tag_type, "mode": mode,
            "total": total, "started_at": started, **counters,
        })

    if dry_run:
        conn.close()
        return {
            "ok": True, "dry_run": True, "total": total,
            "candidate_count": len(candidate_tags),
            "prompt_hash": prompt_hash, "model": model,
        }

    if total == 0:
        write_progress(db_path, {
            "running": False, "phase": "completed",
            "summary": {"total": 0, "assigned": 0, "skipped": 0,
                        "errors": 0, "duration_seconds": 0},
            "tag_type": tag_type, "mode": mode,
        })
        conn.close()
        return {"ok": True, "summary": {"total": 0, "assigned": 0,
                                        "errors": 0, "duration_seconds": 0}}

    _flush_progress()

    # 1 つの sqlite3 接続を全スレッドで共有するのは不安なので、書き込みは
    # メインスレッドで逐次行う。Gemini 呼び出しのみ並列化する。

    def _judge_one(target: dict) -> dict:
        master_fp = target["master_fp"]
        # SQLite はスレッド境界をまたげないので read-only 接続をワーカ毎に開く
        worker_conn = sqlite3.connect(str(db_path), timeout=10)
        worker_conn.row_factory = sqlite3.Row
        try:
            cached = get_cached_judgment(
                worker_conn, master_fp, tag_type, prompt_hash, model,
            )
        finally:
            worker_conn.close()
        if cached is not None:
            return {"master_fp": master_fp, "result": cached, "from_cache": True}
        if not api_key:
            return {"master_fp": master_fp, "error": "GEMINI_API_KEY not set"}
        prompt = render_prompt(
            prompt_template, candidate_tags,
            target.get("detected_scene") or "",
            target.get("ocr") or "",
        )
        parsed, in_tok, out_tok, err = call_gemini(model, prompt, api_key)
        if parsed is None:
            return {"master_fp": master_fp, "error": err or "unknown",
                    "in_tokens": in_tok, "out_tokens": out_tok}
        return {"master_fp": master_fp, "result": parsed,
                "in_tokens": in_tok, "out_tokens": out_tok}

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_judge_one, t) for t in targets]
        last_progress_flush = time.time()
        for fut in futures:
            r = fut.result()
            results.append(r)
            counters["processed"] += 1
            if "from_cache" in r:
                counters["cache_hits"] += 1
            elif "error" in r:
                counters["errors"] += 1
            else:
                counters["api_calls"] += 1
            now = time.time()
            if now - last_progress_flush >= 1.0:
                _flush_progress()
                last_progress_flush = now

    # 結果を DB に書き込み (メインスレッド)
    assigned = 0
    for r in results:
        master_fp = r["master_fp"]
        if "error" in r:
            continue
        result_json = r["result"]
        # キャッシュ保存 (新規取得分のみ)
        if not r.get("from_cache"):
            save_cache(conn, master_fp, tag_type, prompt_hash, model, result_json)
            in_tok = int(r.get("in_tokens", 0))
            out_tok = int(r.get("out_tokens", 0))
            if in_tok or out_tok:
                record_api_usage(model, purpose, in_tok, out_tok, conn=conn)
        apply_judgment(conn, master_fp, version_id, tag_type,
                       result_json, valid_tag_ids, reset_manual)
        assigned += 1
    conn.commit()

    duration = time.time() - started
    summary = {
        "total": total, "assigned": assigned,
        "cache_hits": counters["cache_hits"],
        "api_calls": counters["api_calls"],
        "errors": counters["errors"],
        "duration_seconds": round(duration, 1),
    }
    write_progress(db_path, {
        "running": False, "phase": "completed",
        "tag_type": tag_type, "mode": mode, "summary": summary,
    })
    conn.close()
    return {"ok": True, "summary": summary, "prompt_hash": prompt_hash}


def estimate_targets(
    db_path: Path, tag_type: str, mode: str, reset_manual: bool, version_id: int,
) -> dict:
    """確認モーダル用の推定値を返す (実際の判定はしない)。"""
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    candidate_tags = fetch_candidate_tags(conn, tag_type)
    if not candidate_tags:
        conn.close()
        return {"target_count": 0, "cache_hit_estimate": 0,
                "api_call_estimate": 0, "model": MODEL_BY_TYPE[tag_type]}
    prompt_template = fetch_active_prompt(conn, tag_type)
    prompt_hash = compute_prompt_hash(prompt_template, candidate_tags)
    targets = fetch_target_master_fps(conn, tag_type, mode, reset_manual, version_id)
    target_fps = [t["master_fp"] for t in targets]

    cache_hits = 0
    if target_fps:
        placeholders = ",".join(["?"] * len(target_fps))
        cnt = conn.execute(
            f"SELECT COUNT(*) FROM lc_tag_judgments"
            f" WHERE master_fp IN ({placeholders})"
            f"   AND tag_type = ? AND prompt_hash = ? AND model = ?",
            (*target_fps, tag_type, prompt_hash, MODEL_BY_TYPE[tag_type]),
        ).fetchone()
        cache_hits = int(cnt[0]) if cnt else 0

    # 直近 100 件の API 使用量から平均トークン数を算出 (テーブルが
    # まだ無い場合はフォールバックを使う)
    avg_in, avg_out = 1000, 100
    try:
        row = conn.execute(
            "SELECT AVG(input_tokens), AVG(output_tokens) FROM ("
            "  SELECT input_tokens, output_tokens FROM lc_api_usage"
            "  WHERE model = ? AND purpose = ?"
            "  ORDER BY id DESC LIMIT 100"
            ")",
            (MODEL_BY_TYPE[tag_type], PURPOSE_BY_TYPE[tag_type]),
        ).fetchone()
        if row and row[0] and row[1]:
            avg_in, avg_out = int(row[0]), int(row[1])
    except sqlite3.OperationalError:
        pass

    api_calls = max(0, len(target_fps) - cache_hits)
    conn.close()
    return {
        "target_count": len(target_fps),
        "cache_hit_estimate": cache_hits,
        "api_call_estimate": api_calls,
        "estimated_seconds": round(api_calls / 5.0 * 1.5),
        "estimated_input_tokens_total": api_calls * avg_in,
        "estimated_output_tokens_total": api_calls * avg_out,
        "model": MODEL_BY_TYPE[tag_type],
        "prompt_hash": prompt_hash,
    }


# ─── テスト判定 (5 件サンプル、DB 書き込みなし) ──────────


def test_prompt_with_samples(
    db_path: Path, tag_type: str, prompt_text: str, sample_size: int,
    version_id: int,
) -> dict:
    """プロンプト編集 UI のテスト判定。

    - DB 書き込みなし (判定キャッシュ・タグ付与とも触らない)
    - 5 件サンプル (ランダム選択)
    - api_usage は記録 (purpose='tag_prompt_test')
    """
    from tools.ap.api_usage import record_api_usage

    api_key = os.environ.get("GEMINI_API_KEY", "")
    model = MODEL_BY_TYPE[tag_type]

    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    candidate_tags = fetch_candidate_tags(conn, tag_type)
    if not candidate_tags:
        conn.close()
        return {"ok": False, "error": "no_candidate_tags"}

    rows = conn.execute(
        "SELECT m.master_fp,"
        "  COALESCE(s.ocr_text_gemini, s.ocr_text_hq, s.ocr_text, '') AS ocr,"
        "  m.scene AS detected_scene,"
        "  m.title, s.thumbnail_path, s.screenshot_path"
        " FROM lc_master_nodes m"
        " LEFT JOIN lc_screens s ON s.id = m.representative_screen_id"
        " WHERE m.version_id = ?"
        " ORDER BY RANDOM() LIMIT ?",
        (version_id, sample_size),
    ).fetchall()

    if not rows:
        conn.close()
        return {"ok": True, "samples": [], "duration_seconds": 0}

    started = time.time()
    samples = []
    for r in rows:
        prompt = render_prompt(
            prompt_text, candidate_tags,
            r["detected_scene"] or "", r["ocr"] or "",
        )
        if not api_key:
            samples.append({
                "master_fp": r["master_fp"], "title": r["title"],
                "ocr_text": r["ocr"], "detected_scene": r["detected_scene"],
                "thumbnail_path": r["thumbnail_path"],
                "result": {"error": "GEMINI_API_KEY not set"},
            })
            continue
        parsed, in_tok, out_tok, err = call_gemini(model, prompt, api_key)
        if in_tok or out_tok:
            record_api_usage(model, "tag_prompt_test", in_tok, out_tok, conn=conn)
        samples.append({
            "master_fp": r["master_fp"], "title": r["title"],
            "ocr_text": r["ocr"], "detected_scene": r["detected_scene"],
            "thumbnail_path": r["thumbnail_path"],
            "result": parsed if parsed else {"error": err or "unknown"},
        })

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "samples": samples,
        "duration_seconds": round(time.time() - started, 1),
    }


# ─── CLI エントリ ─────────────────────────────────────


def _main() -> int:
    parser = argparse.ArgumentParser(description="タグ機能 Gemini 判定")
    parser.add_argument("--type", required=True, choices=["scene", "sub_scene"])
    parser.add_argument("--mode", default="unassigned",
                        choices=["unassigned", "all"])
    parser.add_argument("--reset-manual", action="store_true")
    parser.add_argument("--version-id", type=int, default=1)
    parser.add_argument("--db", type=str, default=None,
                        help="DB path (default: crawler/storage/ludus.db)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=5)
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else (
        Path(__file__).parent.parent / "storage" / "ludus.db"
    )

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    result = run_judgment(
        db_path=db_path, tag_type=args.type, mode=args.mode,
        reset_manual=args.reset_manual, version_id=args.version_id,
        dry_run=args.dry_run, max_workers=args.workers,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(_main())
