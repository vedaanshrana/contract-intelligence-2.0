"""
CPI agent adapter — two stages.

STAGE 1  (extraction, ported from find_CPI_and_extract_info.py / CPI.ipynb):
    Scans the client's contract PDFs, finds exact "CPI" mentions (with an
    "increased annually effective" / "Annual Adjustment" fallback), asks an LLM
    to pull the fee-increase effective date and minimum increase per snippet,
    and writes  Output/<ClientName>/<ClientName> CPI_matches.xlsx.

STAGE 2  (formatting, original CPI Final Output.ipynb logic):
    Reshapes that matches file into the standard CPI database output:
    Output/<ClientName>/cpi_output.xlsx

run_full() chains both stages.  Stage 1 is skipped if a matches file already
exists; Stage 2 is always (re)run from the matches file.

OCR (pytesseract + Tesseract binary) and rapidfuzz are OPTIONAL.  Text-based
PDFs are handled without them.  For image-only PDFs where OCR is unavailable or
produces no usable text ("no OCR determination"), the agent falls back to a
gpt-5.2 vision model: it renders the page images and reads them directly using
the same extraction logic (snippet / cpi_effective_date / minimum_fee_increase).
The fallback model is config.CPI_VISION_MODEL.
"""

import base64
import io
import json
import os
import re
import time
import unicodedata
from calendar import month_name
from pathlib import Path
from typing import Callable, Optional

import fitz                       # PyMuPDF
import pandas as pd
from fiserv_client import make_client
from PIL import Image

from config import CPI_API_KEY, CPI_MODEL, CPI_VISION_MODEL

_ADAPTER_DIR = Path(__file__).resolve().parent.parent
_INPUT_DIR   = _ADAPTER_DIR / "Input"
_OUTPUT_DIR  = _ADAPTER_DIR / "Output"

_MONTHS = [m for m in month_name if m]   # ['January', 'February', ...]

# ── Optional dependencies ───────────────────────────────────────────────────
try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

try:
    import pytesseract
    _TESS_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(_TESS_PATH):
        pytesseract.pytesseract.tesseract_cmd = _TESS_PATH
    # If the binary isn't at the default path we still trust PATH; pytesseract
    # raises at call time if it's genuinely missing (handled per-call).
    _HAS_OCR = True
except ImportError:
    _HAS_OCR = False


# ════════════════════════════════════════════════════════════════════════════
# STAGE 1 — CPI EXTRACTION (ported from the notebook)
# ════════════════════════════════════════════════════════════════════════════

_SEARCH_TERM          = "CPI"
_CONTEXT_WINDOW_WORDS = 40
_OPENAI_MAX_TOKENS    = 800

# Per-run text caches (avoid re-OCR of the same file)
_OCR_TEXT_CACHE: dict  = {}
_TEXT_PAGE_CACHE: dict = {}

_SYSTEM_PROMPT = "You are a contract-reading assistant. Extract structured info from the provided snippets."

_USER_PROMPT_SNIPPET_ANALYSIS = """
You are given a list of snippets from a contract where the term "CPI" appears. For each snippet, provide a JSON object with fields:
  - "snippet": the snippet text (return a snippet that makes logical sense, you may trim but keep the relevant CPI sentence(s) and any fees or services related information that may be mentioned). Include information about conditions where the annual increase does not apply, if present.
  - "cpi_effective_date": the CPI effective date mentioned in this snippet if present (return in the form you see it, e.g., "January 1, 2020", "1/1/2020", "01_01_2020" etc.). If none, return empty string "".
  - "minimum_fee_increase": the minimum fee increase percentage or expression mentioned in this snippet if present (e.g., "2%", "at least 1.5%", "no less than 2 percent"). If none, return empty string "".

Return a JSON array of objects, e.g.:
[
  {"snippet":"...","cpi_effective_date":"...","minimum_fee_increase":"..."},
  ...
]

Here are the snippets (numbered). Provide the JSON array ONLY.
<<snippets_block>>
""".strip()

# ── Vision fallback prompts (image-only contracts; no usable OCR text) ────────
_VISION_SYSTEM_PROMPT = (
    "You are a contract-reading assistant. You are shown page images of a "
    "contract (scanned or image-only). Read them carefully — including small "
    "print, tables, fee schedules, and footnotes — and extract structured info "
    "about CPI-based price/fee adjustments."
)

_USER_PROMPT_VISION_CPI = """
The following images are sequential pages of a contract. Read every page carefully.

Find EVERY passage where pricing or fees are tied to "CPI" or the "Consumer Price Index". Also include any "Annual Adjustment ... whichever is greater/lesser" language and any "increased annually effective" fee language, even if the literal term "CPI" is not used in that passage.

For each relevant passage, provide a JSON object with fields:
  - "snippet": the passage text, transcribed from the image (keep the relevant CPI sentence(s) and any fees or services related information; include conditions where the annual increase does not apply, if present).
  - "cpi_effective_date": the CPI/fee-increase effective date mentioned in this passage if present (return in the form you see it, e.g., "January 1, 2020", "1/1/2020", "01_01_2020"). If none, return empty string "".
  - "minimum_fee_increase": the minimum fee increase percentage or expression mentioned in this passage if present (e.g., "2%", "at least 1.5%", "no less than 2 percent"). If none, return empty string "".

Return a JSON array of objects ONLY, e.g.:
[
  {"snippet":"...","cpi_effective_date":"...","minimum_fee_increase":"..."}
]
If no CPI-related language appears in these pages, return an empty array [].
""".strip()

# Vision fallback render/chunk settings.
_VISION_DPI        = 300   # page render resolution for vision fallback
_VISION_CHUNK_SIZE = 10    # pages per vision call (keeps prompts under context limits)

_DATE_RE     = re.compile(r'\b(\d{1,2}[_/]\d{1,2}[_/]\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4}|[A-Za-z]+\s+\d{1,2},\s*\d{4})\b')
_PERCENT_RE  = re.compile(r'(\d+(?:\.\d+)?\s*(?:%|percent|percentage|bps|basis points))', re.IGNORECASE)
_PERCENT_RE2 = re.compile(r'(at least|no less than|not less than|minimum of)?\s*([0-9]+(?:\.[0-9]+)?)\s*(%|percent|percentage)', re.IGNORECASE)


def _normalize_for_match(s: str) -> str:
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = re.sub('[\u200b-\u200f\ufeff\xa0\xad]', '', s)
    return re.sub(r'[^A-Za-z0-9]', '', s).lower().strip()


def _similarity(a: str, b: str) -> float:
    if _HAS_RAPIDFUZZ:
        return fuzz.token_set_ratio(a, b)
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def _ocr_pdf_to_text_pages(pdf_path: Path) -> list:
    """OCR every page of an image-based PDF. Returns [] if OCR is unavailable."""
    if not _HAS_OCR:
        return []
    text_pages = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return []
    for page in doc:
        pix = page.get_pixmap(dpi=300)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            text_pages.append(pytesseract.image_to_string(img))
        except Exception:
            text_pages.append("")
    doc.close()
    return text_pages


def _tokenize_page_words(page) -> list:
    words = page.get_text("words")
    if not words:
        return []
    words_sorted = sorted(words, key=lambda w: (round(w[1], 2), round(w[0], 2)))
    return [{"text": w[4], "norm": _normalize_for_match(w[4])} for w in words_sorted]


def _find_exact_term_locations(term_norm: str, page_tokens: list) -> list:
    return [i for i, tok in enumerate(page_tokens) if tok["norm"] == term_norm]


def _make_snippet(page_tokens: list, match_idx: int, window: int = _CONTEXT_WINDOW_WORDS) -> str:
    start = max(0, match_idx - window)
    end   = min(len(page_tokens) - 1, match_idx + window)
    return " ".join(page_tokens[i]["text"] for i in range(start, end + 1))


def _unique_preserve_order(items: list) -> list:
    seen, out = set(), []
    for it in items:
        if it and it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _normalize_snippet_text(s: str) -> str:
    return re.sub(r'\s+', ' ', (s or "").strip()).lower()


def _dedupe_similar_snippets(snippets: list, threshold: int = 70) -> list:
    if not snippets:
        return []
    kept: list = []
    for s in snippets:
        norm_s = _normalize_snippet_text(s)
        if not norm_s:
            continue
        found = False
        for idx, (ks, kn) in enumerate(kept):
            if _similarity(kn, norm_s) >= threshold:
                if len(s) > len(ks):
                    kept[idx] = (s, norm_s)
                found = True
                break
        if not found:
            kept.append((s, norm_s))
    return [t[0] for t in kept]


def _extract_annual_adjustment_snippets(pdf_path: Path) -> list:
    """Snippets from 'Annual Adjustment' to the first '.' after whichever is greater/lesser."""
    snippets: list = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return snippets

    text_pages = []
    for page in doc:
        txt = page.get_text("text")
        if txt and txt.strip():
            text_pages.append(txt)
        elif _HAS_OCR:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                text_pages.append(pytesseract.image_to_string(img))
            except Exception:
                text_pages.append("")
        else:
            text_pages.append("")
    doc.close()

    for page_index, page_text in enumerate(text_pages):
        lower     = page_text.lower()
        start_idx = lower.find("annual adjustment")
        if start_idx == -1:
            continue
        region      = lower[start_idx:]
        greater_idx = region.find("whichever is greater")
        lesser_idx  = region.find("whichever is lesser")
        if greater_idx != -1 and lesser_idx != -1:
            phrase_idx = min(greater_idx, lesser_idx)
        elif greater_idx != -1:
            phrase_idx = greater_idx
        elif lesser_idx != -1:
            phrase_idx = lesser_idx
        else:
            continue
        phrase_abs = start_idx + phrase_idx
        dot_idx    = lower.find('.', phrase_abs)
        if dot_idx == -1:
            dot_idx = len(page_text)
        snippet = page_text[start_idx:dot_idx + 1].strip()
        snippets.append(f"(File: {pdf_path.name} - Page {page_index + 1}) {snippet}")
    return snippets


def _extract_fallback_increased_annually(pdf_path: Path) -> list:
    """Fallback when 'CPI' not found: look for 'increased annually effective'."""
    target_norm = _normalize_for_match("increased annually effective")
    snippets: list = []
    name = pdf_path.name

    if name in _OCR_TEXT_CACHE:
        pages_text = _OCR_TEXT_CACHE[name]
    elif name in _TEXT_PAGE_CACHE:
        pages_text = _TEXT_PAGE_CACHE[name]
    else:
        try:
            doc = fitz.open(str(pdf_path))
        except Exception:
            return snippets
        pages_text, has_text = [], False
        for page in doc:
            t = page.get_text("text")
            pages_text.append(t)
            if t.strip():
                has_text = True
        doc.close()
        if has_text:
            _TEXT_PAGE_CACHE[name] = pages_text
        else:
            pages_text = _ocr_pdf_to_text_pages(pdf_path)
            _OCR_TEXT_CACHE[name] = pages_text

    for page_index, text in enumerate(pages_text, start=1):
        if not isinstance(text, str):
            continue
        if target_norm in _normalize_for_match(text):
            snippets.append(f"(File: {name} - Page {page_index}) {text.strip()}")
    return snippets


def _local_extract_from_snippets(snippets: list) -> list:
    out = []
    for s in snippets:
        dm = _DATE_RE.search(s)
        pm = _PERCENT_RE.search(s) or _PERCENT_RE2.search(s)
        out.append({
            "snippet": s.strip(),
            "cpi_effective_date": dm.group(0) if dm else "",
            "minimum_fee_increase": pm.group(0).strip() if pm else "",
        })
    return out


def _coerce_snippet_objs(parsed) -> list:
    """Normalize a parsed JSON list into snippet/date/increase dicts."""
    out = []
    for obj in parsed:
        if isinstance(obj, dict):
            out.append({
                "snippet": (obj.get("snippet") or "").strip(),
                "cpi_effective_date": (obj.get("cpi_effective_date") or "").strip(),
                "minimum_fee_increase": (obj.get("minimum_fee_increase") or "").strip(),
            })
    return out


def _parse_snippet_json_array(text: str):
    """Parse an LLM response into a list of snippet dicts, or None on failure.

    Accepts a bare JSON array or a JSON array embedded in surrounding prose.
    """
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return _coerce_snippet_objs(parsed)
    except Exception:
        m = re.search(r'\[.*\]', text, flags=re.DOTALL)
        if m:
            try:
                return _coerce_snippet_objs(json.loads(m.group(0)))
            except Exception:
                pass
    return None


def _call_llm_analyze_snippets(client, snippets: list, model: str,
                               retries: int = 2, wait_secs: int = 2) -> list:
    if not snippets:
        return []
    block = "\n\n".join(f"{i+1}. {s}" for i, s in enumerate(snippets))
    user_prompt = _USER_PROMPT_SNIPPET_ANALYSIS.replace("<<snippets_block>>", block)

    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=_OPENAI_MAX_TOKENS,
                temperature=0.0,
            )
            text = resp.choices[0].message.content.strip()
            parsed = _parse_snippet_json_array(text)
            if parsed is not None:
                return parsed
            return _local_extract_from_snippets(snippets)
        except Exception:
            if attempt < retries:
                time.sleep(wait_secs * (attempt + 1))
            else:
                return _local_extract_from_snippets(snippets)
    return []


# ════════════════════════════════════════════════════════════════════════════
# VISION FALLBACK — read image-only contracts directly with gpt-5.2
# ════════════════════════════════════════════════════════════════════════════
#
# Used when a PDF has no embedded text AND OCR is unavailable or produced no
# usable text ("no OCR determination"). Instead of skipping the contract, we
# render its pages to images and let a vision model read them — same extraction
# logic (snippet / cpi_effective_date / minimum_fee_increase), but the model
# sees the pages rather than OCR text.


def _ocr_pages_have_text(pages) -> bool:
    """True if an OCR result actually contains some text on any page."""
    return bool(pages) and any(isinstance(t, str) and t.strip() for t in pages)


# Lines that appear on scanned / e-signed pages but carry NO contract content:
# DocuSign envelope stamps, bare page numbers, "certified true copy" banners.
# A text layer made only of these fools a naive "has text" check — the real
# contract body (incl. any CPI language) lives in the page images.
_BOILERPLATE_LINE_RE = re.compile(
    r'docusign envelope id'
    r'|^\s*page\s+\d+(\s+of\s+\d+)?\s*$'
    r'|certified\s+true\s+copy',
    re.IGNORECASE,
)


def _meaningful_words(text: str) -> int:
    """Count content words on a page, ignoring e-sign / boilerplate lines."""
    if not isinstance(text, str) or not text.strip():
        return 0
    kept = [ln for ln in text.splitlines() if not _BOILERPLATE_LINE_RE.search(ln)]
    return len(re.findall(r'[A-Za-z]{2,}', " ".join(kept)))


def _is_substantive_text(text_pages: list,
                         min_words_per_page: float = 12.0,
                         min_substantive_fraction: float = 0.30) -> bool:
    """True if the embedded text layer is real contract content rather than just
    e-signature stamps / page furniture.

    Returns False for image-only PDFs (no text) AND for scanned PDFs whose only
    text layer is a DocuSign stamp on every page — both must fall through to OCR
    / the vision fallback.
    """
    if not text_pages:
        return False
    per_page = [_meaningful_words(t) for t in text_pages]
    total = sum(per_page)
    if total == 0:
        return False
    avg = total / len(text_pages)
    substantive_pages = sum(1 for w in per_page if w >= min_words_per_page)
    frac = substantive_pages / len(text_pages)
    return avg >= min_words_per_page or frac >= min_substantive_fraction


def _is_gpt5_family(model_name: str) -> bool:
    """gpt-5 / o-series reject temperature != 1 → the parameter must be omitted."""
    m = (model_name or "").lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3")


def _render_pdf_to_images(pdf_path: Path, dpi: int = _VISION_DPI) -> list:
    """Render every page of a PDF to a PIL image. Returns [] on failure."""
    images: list = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return images
    for page in doc:
        try:
            pix = page.get_pixmap(dpi=dpi)
            images.append(Image.open(io.BytesIO(pix.tobytes("png"))))
        except Exception:
            continue
    doc.close()
    return images


def _image_to_b64(img) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _dedupe_snippet_dicts(items: list, threshold: int = 70) -> list:
    """Dedupe snippet dicts by snippet-text similarity (across vision chunks)."""
    kept: list = []
    for it in items:
        norm = _normalize_snippet_text(it.get("snippet", ""))
        if not norm:
            continue
        if any(_similarity(_normalize_snippet_text(k.get("snippet", "")), norm) >= threshold
               for k in kept):
            continue
        kept.append(it)
    return kept


def _call_vision_on_images(client, images: list, vision_model: str,
                           retries: int = 2, wait_secs: int = 5) -> list:
    """Send a chunk of page images to the vision model and parse the result."""
    if not images:
        return []
    content: list = [{"type": "text", "text": _USER_PROMPT_VISION_CPI}]
    for img in images:
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_image_to_b64(img)}"},
        })
    messages = [
        {"role": "system", "content": _VISION_SYSTEM_PROMPT},
        {"role": "user",   "content": content},
    ]

    for attempt in range(retries + 1):
        try:
            kwargs = {"model": vision_model, "messages": messages}
            # gpt-5 family rejects temperature != 1; omit it (mirrors call_vision).
            if not _is_gpt5_family(vision_model):
                kwargs["temperature"] = 0.0
            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            parsed = _parse_snippet_json_array(text)
            return parsed if parsed is not None else []
        except Exception:
            if attempt < retries:
                time.sleep(wait_secs * (attempt + 1))
            else:
                return []
    return []


def _vision_extract_cpi(client, pdf_path: Path, vision_model: str,
                        log: Callable[[str], None]) -> list:
    """Vision fallback: render the PDF pages and extract CPI snippets directly.

    Returns a list of {snippet, cpi_effective_date, minimum_fee_increase} dicts
    (same shape as _call_llm_analyze_snippets), deduped across page chunks.
    """
    images = _render_pdf_to_images(pdf_path)
    if not images:
        log(f"  ⚠ Vision fallback could not render {pdf_path.name}")
        return []

    results: list = []
    for start in range(0, len(images), _VISION_CHUNK_SIZE):
        chunk = images[start:start + _VISION_CHUNK_SIZE]
        log(f"  Vision reading {pdf_path.name} pages "
            f"{start + 1}-{start + len(chunk)} with {vision_model}…")
        results.extend(_call_vision_on_images(client, chunk, vision_model))

    # Keep only objects that carry some content.
    results = [r for r in results
               if r.get("snippet") or r.get("cpi_effective_date") or r.get("minimum_fee_increase")]
    return _dedupe_snippet_dicts(results, threshold=70)


def _build_vision_row(client, p: Path, vision_model: str,
                      log: Callable[[str], None]) -> dict:
    """Run the vision fallback on one PDF and shape the result into a matches row
    (same columns as the text/OCR path; OCR column tagged 'Vision')."""
    v_out = _vision_extract_cpi(client, p, vision_model, log)
    combined = " ||| ".join(i.get("snippet", "") for i in v_out if i.get("snippet", ""))
    cpi_dates = _unique_preserve_order(
        [i.get("cpi_effective_date", "").strip() for i in v_out if i.get("cpi_effective_date", "").strip()])
    min_incs = _unique_preserve_order(
        [i.get("minimum_fee_increase", "").strip() for i in v_out if i.get("minimum_fee_increase", "").strip()])
    if v_out:
        log(f"  Vision model found {len(v_out)} CPI passage(s) in {p.name}")
    else:
        log(f"  Vision model found no CPI language in {p.name}")
    return {
        "Filename": p.name,
        "Contract Type": _detect_contract_type_from_filename(p.name),
        "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
        "CPI Snippets (LLM)": combined,
        "Fee Increase Effective Date(s)": ", ".join(cpi_dates),
        "Minimum Fee Increase(s)": ", ".join(min_incs),
        "OCR": "Vision",
        "Page Number": "",
    }


def _parse_contract_effective_date_from_filename(name: str) -> str:
    m = re.search(r'(\d{1,2}[_-]\d{1,2}[_-]\d{4})', name)
    return m.group(1) if m else ""


def _detect_contract_type_from_filename(name: str) -> str:
    low = name.lower()
    if "master agreement" in low or "master" in low:
        return "Master Agreement"
    if "amendment" in low or "amend" in low:
        return "Amendment"
    return ""


def _scan_pdfs_in_folder(folder: Path, client, model: str,
                         log: Callable[[str], None],
                         contracts: Optional[list] = None,
                         vision_model: str = "") -> list:
    rows: list = []
    term_norm = _normalize_for_match(_SEARCH_TERM)

    pdfs = list(folder.glob("*.pdf")) + list(folder.glob("*.PDF"))
    pdfs = sorted(set(pdfs), key=lambda p: p.name)

    wanted = {str(c) for c in contracts} if contracts is not None else None

    for p in pdfs:
        # Scope-agent filter (if provided)
        if wanted is not None and p.name not in wanted:
            continue
        low = p.name.lower()
        if not ("master" in low or "amendment" in low or "services" in low):
            continue

        try:
            doc = fitz.open(str(p))
        except Exception as e:
            log(f"  ⚠ Could not open {p.name}: {e}")
            continue

        # Build embedded text pages.
        text_pages = [page.get_text("text") for page in doc]

        # A text layer that is only e-signature stamps / page furniture
        # (e.g. "DocuSign Envelope ID: …" on every page of a scanned contract)
        # is NOT a real text layer — the body lives in the page images. Treat
        # such PDFs, and truly image-only PDFs, as "scanned" so OCR / the vision
        # fallback can run instead of silently finding nothing.
        substantive = _is_substantive_text(text_pages)
        is_scanned  = not substantive

        ocr_text_pages = None
        ocr_used = False
        if substantive:
            _TEXT_PAGE_CACHE[p.name] = text_pages
        elif _HAS_OCR:
            log(f"  {p.name}: no substantive text layer — running OCR")
            ocr_text_pages = _ocr_pdf_to_text_pages(p)
            _OCR_TEXT_CACHE[p.name] = ocr_text_pages
            if _ocr_pages_have_text(ocr_text_pages):
                ocr_used = True
            else:
                ocr_text_pages = None   # OCR produced nothing usable → vision

        # ── Main search: CPI ──
        # Only token-scan when we have usable text (substantive embedded text or
        # successful OCR). A scanned PDF with no usable text falls through to the
        # vision fallback below.
        found_any        = False
        first_match_page = None
        snippets_for_llm = []
        if substantive or ocr_text_pages is not None:
            for page_no in range(len(doc)):
                page = doc[page_no]
                if ocr_text_pages is not None:
                    raw = ocr_text_pages[page_no] if page_no < len(ocr_text_pages) else ""
                    page_tokens = [{"text": w, "norm": _normalize_for_match(w)} for w in raw.split()]
                else:
                    page_tokens = _tokenize_page_words(page)

                matches = _find_exact_term_locations(term_norm, page_tokens)
                if matches and first_match_page is None:
                    first_match_page = page_no + 1
                for mi in matches:
                    found_any = True
                    snippets_for_llm.append(
                        f"(File: {p.name} - Page {page_no+1}) {_make_snippet(page_tokens, mi)}"
                    )
        doc.close()

        # CASE 1 — CPI found
        if found_any:
            log(f"  Analyzing {p.name} ({len(snippets_for_llm)} CPI snippet(s))…")
            snippets_for_llm = _dedupe_similar_snippets(snippets_for_llm, threshold=70)
            l_out = _call_llm_analyze_snippets(client, snippets_for_llm, model)

            combined = " ||| ".join(i.get("snippet", "") for i in l_out) if l_out \
                       else " ||| ".join(snippets_for_llm)

            aa = _extract_annual_adjustment_snippets(p)
            if aa:
                aa = _dedupe_similar_snippets(aa, threshold=70)
                combined = " ||| ".join(aa)

            cpi_dates = _unique_preserve_order(
                [i.get("cpi_effective_date", "").strip() for i in l_out if i.get("cpi_effective_date", "").strip()])
            min_incs = _unique_preserve_order(
                [i.get("minimum_fee_increase", "").strip() for i in l_out if i.get("minimum_fee_increase", "").strip()])

            rows.append({
                "Filename": p.name,
                "Contract Type": _detect_contract_type_from_filename(p.name),
                "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
                "CPI Snippets (LLM)": combined,
                "Fee Increase Effective Date(s)": ", ".join(cpi_dates),
                "Minimum Fee Increase(s)": ", ".join(min_incs),
                "OCR": "Yes" if ocr_used else "No",
                "Page Number": first_match_page,
            })
            continue

        # CASE 2 — fallback
        fb = _extract_fallback_increased_annually(p)
        if fb:
            fb_page = None
            m = re.search(r'Page\s+(\d+)', fb[0])
            if m:
                fb_page = int(m.group(1))
            fb = _dedupe_similar_snippets(fb, threshold=70)
            log(f"  Analyzing {p.name} (fallback snippet(s))…")
            l_out = _call_llm_analyze_snippets(client, fb, model)
            combined  = " ||| ".join(i.get("snippet", "") for i in l_out)
            cpi_dates = _unique_preserve_order(
                [i.get("cpi_effective_date", "").strip() for i in l_out if i.get("cpi_effective_date", "").strip()])
            min_incs = _unique_preserve_order(
                [i.get("minimum_fee_increase", "").strip() for i in l_out if i.get("minimum_fee_increase", "").strip()])
            rows.append({
                "Filename": p.name,
                "Contract Type": _detect_contract_type_from_filename(p.name),
                "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
                "CPI Snippets (LLM)": combined,
                "Fee Increase Effective Date(s)": ", ".join(cpi_dates),
                "Minimum Fee Increase(s)": ", ".join(min_incs),
                "OCR": "Yes" if ocr_used else "No",
                "Page Number": fb_page,
            })
            continue

        # CASE 3 — vision fallback. The text/OCR scan determined nothing AND
        # the PDF is scanned/image-only (incl. e-signed scans whose only text
        # layer is a DocuSign stamp). Render the page images and read them with
        # the vision model — "no OCR determination" no longer means we give up.
        if is_scanned:
            log(f"  {p.name}: text/OCR found no CPI on a scanned PDF — using vision model")
            rows.append(_build_vision_row(client, p, vision_model or CPI_VISION_MODEL, log))
            continue

        # CASE 4 — substantive text PDF with genuinely no CPI language.
        rows.append({
            "Filename": p.name,
            "Contract Type": _detect_contract_type_from_filename(p.name),
            "Contract Effective Date": _parse_contract_effective_date_from_filename(p.name),
            "CPI Snippets (LLM)": "",
            "Fee Increase Effective Date(s)": "",
            "Minimum Fee Increase(s)": "",
            "OCR": "Yes" if ocr_used else "No",
            "Page Number": "",
        })
    return rows


def extract(
    client_name: str,
    api_key: str = "",
    model: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    contracts: Optional[list] = None,
    core: str = "",
    vision_model: str = "",
) -> dict:
    """
    Stage 1 — scan the client's PDFs and write <ClientName> CPI_matches.xlsx.

    contracts:    optional allowlist of PDF filenames (from the scope agent).
                  When None, every Master/Amendment/Services PDF is scanned.
    vision_model: gpt-5.2-class model used as the fallback for image-only PDFs
                  where OCR is unavailable or fails. Defaults to CPI_VISION_MODEL.

    Returns {status, client, rows, output}.
    """
    _OCR_TEXT_CACHE.clear()
    _TEXT_PAGE_CACHE.clear()

    folder = (_INPUT_DIR / core / client_name) if core else (_INPUT_DIR / client_name)
    if not folder.exists():
        return {"status": "no_folder", "client": client_name}

    log    = progress_callback or (lambda msg: None)
    client = make_client(api_key or CPI_API_KEY)

    log(f"Scanning PDFs for CPI language in {client_name}…")
    rows = _scan_pdfs_in_folder(folder, client, model or CPI_MODEL, log,
                                contracts=contracts,
                                vision_model=vision_model or CPI_VISION_MODEL)

    cols = ["Filename", "Contract Type", "Contract Effective Date", "CPI Snippets (LLM)",
            "Fee Increase Effective Date(s)", "Minimum Fee Increase(s)", "OCR", "Page Number"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)

    out_path = _OUTPUT_DIR / client_name / f"{client_name} CPI_matches.xlsx"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(str(out_path), index=False)
    log(f"Wrote CPI matches: {out_path.name} ({len(df)} rows)")

    return {"status": "complete", "client": client_name, "rows": len(df), "output": str(out_path)}


# ════════════════════════════════════════════════════════════════════════════
# STAGE 2 — CPI FORMATTING (original CPI Final Output.ipynb logic)
# ════════════════════════════════════════════════════════════════════════════

def _trim_leading_paren(text):
    if pd.isna(text): return text
    s = str(text).lstrip()
    if s.startswith("("):
        idx = s.find(")")
        if idx != -1: return s[idx + 1:].strip()
    return s


def _extract_client_name(filename):
    if pd.isna(filename): return ""
    parts = []
    for chunk in str(filename).split():
        parts.extend(chunk.split("-"))
    return " ".join(
        p for p in parts
        if any(c.isalpha() for c in p) and p == p.upper() and p.upper() != "PDF"
    )


def _extract_year(text):
    if pd.isna(text): return ""
    s = str(text)
    m = re.search(r"\b(20\d{2})\b", s)
    if m: return m.group(1)
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = m.group(3)
        return str(2000 + int(y)) if len(y) == 2 else y
    return ""


def _extract_month(text):
    if pd.isna(text): return ""
    s = str(text)
    for mo in _MONTHS:
        if mo.lower() in s.lower(): return mo
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 12: return month_name[n]
    return ""


def _contract_year_plus_one(text):
    if pd.isna(text): return ""
    s     = str(text).strip()
    last4 = s[-4:] if len(s) >= 4 else ""
    return str(int(last4) + 1) if last4.isdigit() else ""


def _compute_cpi_terms(row):
    snippet = row.get("CPI Snippets (LLM)")
    if pd.isna(snippet) or str(snippet).strip() == "":
        return "CPI Language not Found"
    s   = str(snippet)
    pct = re.findall(r"(\d+(?:\.\d+)?)\s*(%|percent)", s, re.I)
    min_fee = ", ".join(f"{m[0]}%" for m in pct) if pct else "NA"
    if "whichever is greater" in s.lower():
        return f"> CPI-U or {min_fee}"
    if "cpi" not in s.lower() and "consumer price index" not in s.lower():
        return f"Max {min_fee}"
    return "Limited to CPI-U" if min_fee == "NA" else f"< CPI-U or {min_fee}"


def _compute_elig_year(row):
    if pd.isna(row.get("CPI Snippets (LLM)")) or str(row.get("CPI Snippets (LLM)")).strip() == "":
        return ""
    y = _extract_year(row.get("Fee Increase Effective Date(s)"))
    return y if y else _contract_year_plus_one(row.get("Contract Effective Date"))


def _split_lang(snippet):
    if pd.isna(snippet) or str(snippet).strip() == "": return "", ""
    s = str(snippet)
    return (s, "") if "30" in s else ("", s)


def _specific_lang(snippet):
    if pd.isna(snippet) or str(snippet).strip() == "": return ""
    s   = str(snippet)
    idx = s.lower().rfind("limited")
    if idx != -1: return s[idx:].strip()
    return s.strip() if "30" in s else ""


def run(
    client_name: str,
    cpi_matches_path: Path,
    core_name: str = "",
) -> dict:
    """
    Stage 2 — format a CPI matches Excel file into cpi_output.xlsx.
    """
    if not Path(cpi_matches_path).exists():
        return {"status": "no_input", "client": client_name}

    df = pd.read_excel(str(cpi_matches_path))
    if df.empty:
        # No PDFs produced any rows — still emit an empty formatted file.
        out_path = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame().to_excel(str(out_path), index=False)
        return {"status": "complete", "client": client_name, "rows": 0, "output": str(out_path)}

    df["Filename"]            = df["Filename"].astype(str).str[:-4]
    df["CPI Snippets (LLM)"]  = df["CPI Snippets (LLM)"].apply(_trim_leading_paren)

    out = pd.DataFrame()
    out["Client Name"]           = df["Filename"].apply(_extract_client_name)
    out["Core"]                  = core_name or ""
    out["Contract Type"]         = df["Contract Type"]
    out["Contract Effective Date"] = df["Contract Effective Date"].astype(str).str.replace("_", "-", regex=False)
    out["CPI Terms (per Contract)"] = df.apply(_compute_cpi_terms, axis=1)
    out["CPI Eligibility Year"]  = df.apply(_compute_elig_year, axis=1)
    out["CPI Eligibility Month"] = df["Fee Increase Effective Date(s)"].apply(_extract_month)
    out["Notice Requirement"]    = df["CPI Snippets (LLM)"].apply(
        lambda s: "30 Days" if not pd.isna(s) and str(s).strip() else ""
    )
    out["Specific Contract Language/Information"] = df["CPI Snippets (LLM)"].apply(_specific_lang)

    normal_col, review_col = [], []
    for s in df["CPI Snippets (LLM)"]:
        n, r = _split_lang(s)
        normal_col.append(n); review_col.append(r)
    out["Contract Language/Information"]             = normal_col
    out["Contract Language/Information (For Review)"] = review_col
    out["Item Type"] = "Item"
    out["Path"]      = "sites/fss/CPI/Lists/CU CPI Database"

    for col in ("OCR", "Page Number"):
        if col in df.columns: out[col] = df[col]

    output_path = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_excel(str(output_path), index=False)

    return {
        "status": "complete",
        "client": client_name,
        "rows":   len(out),
        "output": str(output_path),
    }


# ════════════════════════════════════════════════════════════════════════════
# PIPELINE HELPERS
# ════════════════════════════════════════════════════════════════════════════

def run_full(
    client_name: str,
    api_key: str = "",
    model: str = "",
    core_name: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
    force_extract: bool = False,
    contracts: Optional[list] = None,
    core: str = "",
    vision_model: str = "",
) -> dict:
    """
    Run both stages: extract CPI matches from PDFs (Stage 1) then format (Stage 2).
    If a matches file already exists and force_extract is False, Stage 1 is skipped.

    contracts:    optional allowlist of PDF filenames (from the scope agent).
    vision_model: gpt-5.2-class fallback model for image-only PDFs where OCR is
                  unavailable or fails. Defaults to CPI_VISION_MODEL.

    A matches file that found NO CPI and never tried the vision fallback is
    treated as stale and re-extracted, so previously-failed clients pick up the
    vision capability automatically (it self-stabilizes once vision has run).
    """
    log = progress_callback or (lambda msg: None)

    matches = find_cpi_input(client_name)
    stale = matches is not None and not force_extract and _matches_is_stale(matches)
    if matches is None or force_extract or stale:
        if stale:
            log("Existing CPI matches found no CPI and predate the vision "
                "fallback — re-extracting with the vision model…")
        ext = extract(client_name, api_key=api_key, model=model,
                      progress_callback=progress_callback, contracts=contracts,
                      core=core, vision_model=vision_model)
        if ext.get("status") != "complete":
            return ext
        matches = Path(ext["output"])
    else:
        log(f"Using existing CPI matches file: {matches.name}")

    log("Formatting CPI output…")
    return run(client_name, matches, core_name=core_name)


def is_processed(client_name: str) -> bool:
    p = _OUTPUT_DIR / client_name / "cpi_output.xlsx"
    return p.exists() and p.stat().st_size > 1_000


def _matches_is_stale(matches_path: Path) -> bool:
    """True if a matches file is worth re-extracting: it found NO CPI language
    for any contract AND the vision fallback was never tried (OCR != 'Vision').

    Once vision has run, rows carry OCR == 'Vision' (even when it legitimately
    finds nothing), so this returns False and the result is reused — no loop.
    """
    try:
        df = pd.read_excel(str(matches_path))
    except Exception:
        return False
    if df.empty:
        return True
    snip = df.get("CPI Snippets (LLM)")
    if snip is not None:
        s = snip.astype(str).str.strip()
        any_cpi = ((s != "") & (s.str.lower() != "nan")).any()
    else:
        any_cpi = False
    ocr = df.get("OCR")
    vision_tried = ocr is not None and ocr.astype(str).str.strip().str.lower().eq("vision").any()
    return (not any_cpi) and (not vision_tried)


def find_cpi_input(client_name: str) -> Optional[Path]:
    """
    Search for a CPI matches file for this client in Output/ and Input/ dirs.
    Returns the first match or None.
    """
    search_dirs = [
        _OUTPUT_DIR / client_name,
        _INPUT_DIR  / client_name,
    ]
    patterns = [
        "*CPI*matches*.xlsx",
        "*cpi*matches*.xlsx",
        "*CPI*.xlsx",
        "*cpi*.xlsx",
    ]
    for d in search_dirs:
        if not d.exists(): continue
        for pat in patterns:
            hits = list(d.glob(pat))
            if hits: return hits[0]
    return None
