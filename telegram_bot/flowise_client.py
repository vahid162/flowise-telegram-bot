# flowise_client.py
# -- Flowise REST client (blocking with retries) --
import json, time, requests
import os



def _t(key: str) -> str:
    """
    Lazy-import برای شکستن چرخه‌ی ایمپورت:
    در زمان فراخوانی، اگر messages_service.t در دسترس بود همان را استفاده می‌کنیم؛
    در غیر این‌صورت، متن پیش‌فرض فارسی برمی‌گردانیم.
    """
    try:
        # فقط وقتی نیاز شد ایمپورت کن؛ در import-time هیچ وابستگی ایجاد نکن
        from messages_service import t as _tx
        return _tx(key)
    except Exception:
        _fallback = {
            "errors.ai.invalid_response": "پاسخ نامعتبر از سرور دریافت شد.",
            "errors.ai.invalid_response_short": "پاسخ نامعتبر از سرور.",
            "errors.ai.unreachable": "خطا در ارتباط با سرور هوش مصنوعی.",
            "errors.ai.missing_base_url": "خطا: FLOWISE_BASE_URL نامشخص است.",
            "errors.ai.missing_chatflow": "خطا: chatflow_id تعیین نشده.",
        }
        return _fallback.get(key, key)



# --- replace whole function: call_flowise(...) ---
def call_flowise(
    base_url: str | None = None,
    api_key: str | None = None,
    chatflow_id: str | None = None,
    question: str = "",
    session_id: str = "",
    timeout_sec: int = 60,
    retries: int = 3,
    backoff_base_ms: int = 400,
    namespace: str | None = None,          # <-- NEW: optional RAG namespace
) -> tuple[str, int | None]:
    """
    تماس ساده با Flowise (سازگار با قبل) + پشتیبانی اختیاری از vars.namespace برای ایزوله‌سازی RAG.
    """
    import json, time, requests, os
    B = (base_url or os.getenv("FLOWISE_BASE_URL","")).rstrip("/")
    if not B:
        return (_t("errors.ai.missing_base_url"), None)
    CF = chatflow_id or os.getenv("CHATFLOW_ID")
    if not CF:
        return (_t("errors.ai.missing_chatflow"), None)
    K = api_key or os.getenv("FLOWISE_API_KEY")
    H = {"Content-Type":"application/json"}
    if K:
        H["Authorization"] = f"Bearer {K}"

    payload = {
        "question": question,
        "overrideConfig": {
            "sessionId": session_id,
            "returnSourceDocuments": True
        }
    }
    # NEW: pass vars.namespace if provided (for strict multi-tenant RAG isolation)
    if namespace:
        payload["overrideConfig"]["vars"] = {"namespace": namespace}

    url = f"{B}/api/v1/prediction/{CF}"

    for attempt in range(1, retries + 1):
        try:
            r = requests.post(
                url, headers=H, data=json.dumps(payload),
                timeout=(4, max(10, int(timeout_sec or 60)))
            )
            r.raise_for_status()
            data = r.json()

            text = None
            if isinstance(data, dict):
                if data.get("text"):
                    text = data["text"]
                else:
                    res = data.get("result")
                    if isinstance(res, dict) and res.get("text"):
                        text = res["text"]
                    elif isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("text"):
                        text = res[0]["text"]

            src = data.get("sourceDocuments") if isinstance(data, dict) else None
            src_count = len(src) if isinstance(src, list) else None

            return (text or _t("errors.ai.invalid_response"), src_count)
        except Exception as e:
            if attempt < retries:
                backoff_ms = backoff_base_ms * (2 ** (attempt - 1))
                time.sleep(backoff_ms / 1000.0)
            else:
                break
    return (_t("errors.ai.unreachable"), None)


def chat_infer(
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    chat_id: int,
    user_id: int,
    text: str,
    namespace: str | None = None,
    session_id: str | None = None,
    chatflow_id: str | None = None,
    timeout_sec: int = 60,
    retries: int = 3,
    backoff_base_ms: int = 400,
    extra_vars: dict | None = None,
) -> tuple[str, int | None]:
    """
    تماس عمومی برای چت چندسازمانی:
      - sessionId: g:<chat_id>:u:<user_id>  (در PV روی گروه فعال هم همین الگو)
      - overrideConfig.vars: شامل namespace و سایر vars مفید
      - chatflow_id: از ورودی یا ENV (MULTITENANT_CHATFLOW_ID → CHATFLOW_ID)
    """
    import json, time, requests
    B = (base_url or os.getenv("FLOWISE_BASE_URL","")).rstrip("/")
    if not B:
        return (_t("errors.ai.missing_base_url"), None)
    CF = chatflow_id or os.getenv("MULTITENANT_CHATFLOW_ID") or os.getenv("CHATFLOW_ID")
    if not CF:
        return (_t("errors.ai.missing_chatflow"), None)
    K = api_key or os.getenv("FLOWISE_API_KEY")
    H = {"Content-Type":"application/json"}
    if K:
        H["Authorization"] = f"Bearer {K}"
    sid = session_id or f"g:{chat_id}:u:{user_id}"
    ns  = namespace or f"grp:{chat_id}"
    payload = {
        "question": text,
        "overrideConfig": {
            "sessionId": sid,
            "returnSourceDocuments": True,
            "vars": {"namespace": ns}
        }
    }
    if extra_vars:
        try:
            payload["overrideConfig"]["vars"].update(dict(extra_vars))
        except Exception:
            pass
    url = f"{B}/api/v1/prediction/{CF}"
    for attempt in range(1, retries+1):
        try:
            r = requests.post(
                url, headers=H, data=json.dumps(payload),
                timeout=(4, max(10, int(timeout_sec or 60)))
            )
            r.raise_for_status()
            data = r.json()
            txt = None
            if isinstance(data, dict):
                if data.get("text"):
                    txt = data["text"]
                else:
                    res = data.get("result")
                    if isinstance(res, dict) and res.get("text"):
                        txt = res["text"]
                    elif isinstance(res, list) and res and isinstance(res[0], dict) and res[0].get("text"):
                        txt = res[0]["text"]
            src = data.get("sourceDocuments") if isinstance(data, dict) else None
            cnt = len(src) if isinstance(src, list) else None
            return (txt or _t("errors.ai.invalid_response_short"), cnt)
        except Exception as e:
            if attempt < retries:
                time.sleep((backoff_base_ms * (2 ** (attempt - 1))) / 1000.0)
            else:
                break
    return (_t("errors.ai.unreachable"), None)   



# --- ADD: health ping for Flowise (compatible, no keyword-only params) ---
def ping_flowise(base_url, chatflow_id, api_key=None, timeout_sec=8, extra_vars=None):
    """
    Lightweight health-call to Flowise prediction endpoint.
    Returns: (ok: bool, elapsed_ms: int, err: str)
    """
    import time, json, requests
    t0 = time.perf_counter()

    # ایمنی: اگر ID نداریم، برگرد
    if not chatflow_id:
        return False, 0, "missing_chatflow_id"

    url = f"{str(base_url).rstrip('/')}/api/v1/prediction/{chatflow_id}"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # بسیاری از chatflowها حداقل یک var می‌خواهند؛ یک مقدار پیش‌فرض امن بفرستیم
    vars_obj = {"namespace": "health"}
    if isinstance(extra_vars, dict):
        try:
            vars_obj.update({k: v for (k, v) in extra_vars.items() if v is not None})
        except Exception:
            pass

    payload = {
        "question": "ping",
        "overrideConfig": {"sessionId": "health:check", "vars": vars_obj}
    }

    try:
        r = requests.post(
            url, headers=headers, data=json.dumps(payload),
            timeout=(4, max(10, int(timeout_sec or 60)))
        )
        ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code == 200:
            return True, ms, ""
        body = r.text
        if isinstance(body, str) and len(body) > 160:
            body = body[:160] + "..."
        return False, ms, f"HTTP {r.status_code} — {body}"
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        return False, ms, f"{type(e).__name__}: {e}"

# --- END ADD ---

