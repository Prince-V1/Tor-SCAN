from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import requests, re, os, sqlite3, json, tempfile
from datetime import datetime
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

app = FastAPI(title="ScanMET API", version="3.2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

OCR_SPACE_KEY = os.getenv("OCR_SPACE_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
DB = os.getenv("SCANMET_DB", "scanmet.db")

RULES = [
    (
        "Product Name",
        r"(?:PRODUCT\s+NAME|PRODUCT)\s*[:\-]?\s*[A-Za-z][A-Za-z0-9 .,'&()/-]{2,}",
        "Product identification was not detected."
    ),
    (
        "MRP",
        r"(?:MRP|MAXIMUM\s+RETAIL\s+PRICE)[^₹0-9]{0,30}(?:₹|RS\.?|INR)?\s*[\d,]+(?:\.\d{1,2})?",
        "MRP declaration was not detected."
    ),
    (
        "Net Quantity",
        r"(?:NET(?:\s+QUANTITY)?|NET\s+WT|NET\s+WEIGHT)\s*[:\-]?\s*\d+(?:\.\d+)?\s*(?:g|kg|mg|ml|l|L)\b|\b\d+(?:\.\d+)?\s*(?:g|kg|mg|ml|l|L)\b",
        "Net quantity/weight was not detected."
    ),
    (
        "Manufacturing Date",
        r"(?:MFD|MFG|PKD|PACKED|MANUFACTURED|DATE\s+OF\s+(?:MFG|MANUFACTURE|PACKING))\s*[:\-]?[^\n]{0,35}",
        "Manufacturing/packing date was not detected."
    ),
    (
        "Expiry / Best Before",
        r"(?:BEST\s+BEFORE|EXP(?:IRY|\.)?|USE\s+BY)\s*[:\-]?[^\n]{0,35}",
        "Expiry / Best Before information was not detected."
    ),
    (
        "Batch / Lot",
        r"(?:BATCH|LOT)\s*(?:NO\.?|NUMBER)?\s*[:\-]?\s*[A-Z0-9][A-Z0-9\-/]{1,}",
        "Batch/Lot identification was not detected."
    ),
    (
        "Manufacturer Details",
        r"(?:MANUFACTURED\s+BY|MANUFACTURER|MFD\.?\s+BY|MARKETED\s+BY|IMPORTED\s+BY|IMPORTER)",
        "Manufacturer/marketer/importer information was not detected."
    ),
    (
        "Consumer Care",
        r"(?:CONSUMER\s+CARE|CUSTOMER\s+CARE|CONSUMER\s+COMPLAINT|HELPLINE|TOLL\s*[- ]?FREE|CONTACT\s+US)",
        "Consumer-care/contact information was not detected."
    ),
]


def init_db():
    con = sqlite3.connect(DB)

    con.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scanned_at TEXT NOT NULL,
            filename TEXT,
            status TEXT NOT NULL,
            score INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            total INTEGER NOT NULL,
            fields TEXT NOT NULL,
            checks TEXT NOT NULL,
            issues TEXT NOT NULL,
            suggestions TEXT NOT NULL,
            ocr TEXT
        )
    """)

    cols = {
        r[1]
        for r in con.execute("PRAGMA table_info(scans)").fetchall()
    }

    for name, typ, default in [
        ("score", "INTEGER", "0"),
        ("passed", "INTEGER", "0"),
        ("total", "INTEGER", "8"),
        ("checks", "TEXT", "[]")
    ]:
        if name not in cols:
            con.execute(
                f"ALTER TABLE scans ADD COLUMN {name} {typ} DEFAULT {default}"
            )

    con.commit()
    con.close()


init_db()


# =========================
# OCR.SPACE
# =========================

def get_ocr(image: bytes):
    if not OCR_SPACE_KEY:
        raise HTTPException(
            status_code=500,
            detail="OCR_SPACE_KEY is not configured on the backend."
        )

    url = "https://api.ocr.space/parse/image"

    files = {
        "file": ("image.jpg", image, "image/jpeg")
    }

    data = {
        "apikey": OCR_SPACE_KEY,
        "language": "eng",
        "isOverlayRequired": "false",
        "OCREngine": "2",
        "isTable": "false",
        "scale": "true"
    }

    try:
        response = requests.post(
            url,
            files=files,
            data=data,
            timeout=60
        )

        if not response.ok:
            raise HTTPException(
                status_code=502,
                detail=f"OCR service returned HTTP {response.status_code}."
            )

        result = response.json()

    except HTTPException:
        raise

    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach OCR service: {exc.__class__.__name__}."
        )

    except ValueError:
        raise HTTPException(
            status_code=502,
            detail="OCR service returned an invalid response."
        )

    if result.get("IsErroredOnProcessing"):
        error_message = result.get("ErrorMessage")

        if isinstance(error_message, list):
            error_message = " ".join(str(x) for x in error_message)

        raise HTTPException(
            status_code=502,
            detail=error_message or "OCR processing failed."
        )

    parsed_results = result.get("ParsedResults", [])

    if not parsed_results:
        return "", None

    text = "\n".join(
        item.get("ParsedText", "")
        for item in parsed_results
    ).strip()

    return text, None


# =========================
# RULE ENGINE
# =========================

def run_rules(text: str):
    fields, checks, issues, suggestions = {}, [], [], []

    text = text or ""

    for name, pattern, message in RULES:
        m = re.search(pattern, text, re.I)

        detected = m.group(0).strip() if m else "Not found"
        passed = bool(m)

        fields[name] = detected

        checks.append({
            "field": name,
            "detected": detected,
            "status": "PASS" if passed else "MISSING"
        })

        if not passed:
            issues.append(message)
            suggestions.append(
                f"Add or verify a clear {name} declaration "
                "in the prescribed label area/format."
            )

    passed_count = sum(
        c["status"] == "PASS"
        for c in checks
    )

    total = len(checks)

    score = (
        round(passed_count / total * 100)
        if total
        else 0
    )

    status = (
        "COMPLIANT"
        if passed_count == total
        else "NON-COMPLIANT"
    )

    return (
        fields,
        checks,
        issues,
        suggestions,
        status,
        score,
        passed_count,
        total
    )


# =========================
# DATABASE
# =========================

def save_scan(
    filename,
    status,
    score,
    passed,
    total,
    fields,
    checks,
    issues,
    suggestions,
    ocr
):
    con = sqlite3.connect(DB)

    cur = con.execute(
        """
        INSERT INTO scans
        (
            scanned_at,
            filename,
            status,
            score,
            passed,
            total,
            fields,
            checks,
            issues,
            suggestions,
            ocr
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            filename,
            status,
            score,
            passed,
            total,
            json.dumps(fields, ensure_ascii=False),
            json.dumps(checks, ensure_ascii=False),
            json.dumps(issues, ensure_ascii=False),
            json.dumps(suggestions, ensure_ascii=False),
            ocr or ""
        )
    )

    con.commit()

    scan_id = cur.lastrowid

    con.close()

    return scan_id


def build_result(scan_id, filename, ocr, confidence):
    (
        fields,
        checks,
        issues,
        suggestions,
        status,
        score,
        passed,
        total
    ) = run_rules(ocr)

    return {
        "id": scan_id,
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "filename": filename,
        "status": status,
        "score": score,
        "passed": passed,
        "total": total,
        "fields": fields,
        "checks": checks,
        "issues": issues,
        "suggestions": suggestions,
        "ocr_text": ocr,
        "ocr_confidence": confidence
    }


# =========================
# BASIC ROUTES
# =========================

@app.get("/")
def home():
    return {
        "app": "ScanMET",
        "version": "3.2",
        "message": "API is running"
    }


@app.get("/health")
def health():
    return {
        "ok": True,
        "ocr_configured": bool(OCR_SPACE_KEY),
        "gemini_configured": bool(GEMINI_KEY),
        "version": "3.2"
    }


# =========================
# BARCODE
# =========================

@app.get("/barcode/{code}")
def barcode(code: str):
    code = re.sub(r"\D", "", code)

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Invalid barcode."
        )

    url = f"https://world.openfoodfacts.org/api/v3.6/product/{code}.json"

    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "ScanMET/3.2 (prototype)"
            },
            timeout=10
        )

        if not r.ok:
            raise HTTPException(
                status_code=502,
                detail="Open Food Facts request failed."
            )

        d = r.json()
        product = d.get("product", {})

        return {
            "found": d.get("status") == 1,
            "barcode": code,
            "name": (
                product.get("product_name")
                or product.get("product_name_en")
                or ""
            ),
            "brand": product.get("brands", ""),
            "quantity": product.get("quantity", ""),
            "image": product.get("image_front_url", ""),
            "ingredients": product.get("ingredients_text", ""),
            "nutrition_grade": (
                product.get("nutriscore_grade", "")
                or product.get("nutrition_grades", "")
            )
        }

    except HTTPException:
        raise

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Barcode lookup failed: {e.__class__.__name__}."
        )


# =========================
# GEMINI EXPLANATION
# =========================

@app.post("/explain")
async def explain(data: dict):

    if not GEMINI_KEY:
        raise HTTPException(
            status_code=503,
            detail=(
                "GEMINI_API_KEY is not configured. "
                "Add it in Render to enable AI explanations."
            )
        )

    issues = data.get("issues", [])

    if not issues:
        return {
            "text": (
                "No missing fields were detected "
                "by the configured ScanMET rules."
            )
        }

    prompt = (
        "You are an assistant inside ScanMET. "
        "Explain these label-screening findings briefly. "
        "Do not decide legal compliance, invent regulations, "
        "or claim certification. Give practical manual-review "
        "suggestions. Findings:\n"
        + "\n".join(f"- {x}" for x in issues)
    )

    url = (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/gemini-3.8-flash:generateContent"
    )

    try:
        r = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_KEY,
                "Content-Type": "application/json"
            },
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "text": prompt
                            }
                        ]
                    }
                ]
            },
            timeout=30
        )

        if not r.ok:
            raise HTTPException(
                status_code=502,
                detail="Gemini rejected the explanation request."
            )

        d = r.json()

        text = ""

        for c in d.get("candidates", []):
            for part in c.get("content", {}).get("parts", []):
                text += part.get("text", "")

        return {
            "text": text.strip() or "No explanation returned."
        }

    except HTTPException:
        raise

    except requests.RequestException as e:
        raise HTTPException(
            status_code=502,
            detail=f"Gemini request failed: {e.__class__.__name__}."
        )


# =========================
# SINGLE SCAN
# =========================

@app.post("/scan")
async def scan(file: UploadFile = File(...)):

    if (
        not file.content_type
        or not file.content_type.startswith("image/")
    ):
        raise HTTPException(
            status_code=400,
            detail="Please upload an image file."
        )

    image = await file.read()

    if not image:
        raise HTTPException(
            status_code=400,
            detail="Uploaded image is empty."
        )

    if len(image) > 10 * 1024 * 1024:
        raise HTTPException(
            status_code=400,
            detail="Image is too large. Maximum size is 10 MB."
        )

    text, confidence = get_ocr(image)

    result = build_result(
        0,
        file.filename or "image",
        text,
        confidence
    )

    scan_id = save_scan(
        file.filename or "image",
        result["status"],
        result["score"],
        result["passed"],
        result["total"],
        result["fields"],
        result["checks"],
        result["issues"],
        result["suggestions"],
        text
    )

    result["id"] = scan_id

    return result


# =========================
# BATCH SCAN
# =========================

@app.post("/scan-batch")
async def scan_batch(files: list[UploadFile] = File(...)):

    if len(files) > 10:
        raise HTTPException(
            status_code=400,
            detail="Maximum 10 labels per batch."
        )

    results = []

    for file in files:

        if (
            not file.content_type
            or not file.content_type.startswith("image/")
        ):
            continue

        image = await file.read()

        if (
            not image
            or len(image) > 10 * 1024 * 1024
        ):
            continue

        text, confidence = get_ocr(image)

        result = build_result(
            0,
            file.filename or "image",
            text,
            confidence
        )

        scan_id = save_scan(
            file.filename or "image",
            result["status"],
            result["score"],
            result["passed"],
            result["total"],
            result["fields"],
            result["checks"],
            result["issues"],
            result["suggestions"],
            text
        )

        result["id"] = scan_id

        results.append(result)

    return {
        "count": len(results),
        "results": results
    }


# =========================
# HISTORY
# =========================

def row_to_result(r):
    return {
        "id": r[0],
        "scanned_at": r[1],
        "filename": r[2],
        "status": r[3],
        "score": r[4],
        "passed": r[5],
        "total": r[6],
        "fields": json.loads(r[7] or "{}"),
        "checks": json.loads(r[8] or "[]"),
        "issues": json.loads(r[9] or "[]"),
        "suggestions": json.loads(r[10] or "[]"),
        "ocr_text": r[11] or "",
        "ocr_confidence": None
    }


@app.get("/history")
def history(limit: int = 50):

    limit = max(1, min(limit, 100))

    con = sqlite3.connect(DB)

    rows = con.execute(
        """
        SELECT
            id,
            scanned_at,
            filename,
            status,
            score,
            passed,
            total,
            fields,
            checks,
            issues,
            suggestions,
            ocr
        FROM scans
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()

    con.close()

    return [
        row_to_result(r)
        for r in rows
    ]


@app.get("/history/{scan_id}")
def history_item(scan_id: int):

    con = sqlite3.connect(DB)

    r = con.execute(
        """
        SELECT
            id,
            scanned_at,
            filename,
            status,
            score,
            passed,
            total,
            fields,
            checks,
            issues,
            suggestions,
            ocr
        FROM scans
        WHERE id=?
        """,
        (scan_id,)
    ).fetchone()

    con.close()

    if not r:
        raise HTTPException(
            status_code=404,
            detail="Scan not found."
        )

    return row_to_result(r)


@app.delete("/history")
def clear_history():

    con = sqlite3.connect(DB)

    con.execute("DELETE FROM scans")

    con.commit()
    con.close()

    return {
        "ok": True
    }


# =========================
# PDF REPORT
# =========================

@app.post("/report")
async def report(data: dict):

    fd, path = tempfile.mkstemp(
        suffix=".pdf"
    )

    os.close(fd)

    pdf = canvas.Canvas(
        path,
        pagesize=A4
    )

    width, height = A4
    y = height - 55

    pdf.setFont(
        "Helvetica-Bold",
        20
    )

    pdf.drawString(
        45,
        y,
        "ScanMET Compliance Screening Report"
    )

    y -= 28

    pdf.setFont(
        "Helvetica",
        10
    )

    pdf.drawString(
        45,
        y,
        f"Scan ID: {data.get('id', '-')}    "
        f"File: {data.get('filename', '-')}"
    )

    y -= 16

    pdf.drawString(
        45,
        y,
        f"Status: {data.get('status', '-')}    "
        f"Score: {data.get('passed', 0)}/"
        f"{data.get('total', 8)} "
        f"({data.get('score', 0)}%)"
    )

    y -= 28

    pdf.setFont(
        "Helvetica-Bold",
        13
    )

    pdf.drawString(
        45,
        y,
        "Field Checks"
    )

    y -= 20

    pdf.setFont(
        "Helvetica",
        9
    )

    for c in data.get("checks", []):

        line = (
            f"{c.get('field', '-')}: "
            f"{c.get('detected', '-')} "
            f"[{c.get('status', '-')}]"
        )

        pdf.drawString(
            55,
            y,
            line[:115]
        )

        y -= 15

        if y < 70:
            pdf.showPage()
            y = height - 55
            pdf.setFont("Helvetica", 9)

    y -= 8

    pdf.setFont(
        "Helvetica-Bold",
        12
    )

    pdf.drawString(
        45,
        y,
        "Issues / Corrections"
    )

    y -= 18

    pdf.setFont(
        "Helvetica",
        9
    )

    for item in data.get("suggestions") or ["No detected issues."]:

        pdf.drawString(
            55,
            y,
            ("• " + str(item))[:115]
        )

        y -= 15

        if y < 70:
            pdf.showPage()
            y = height - 55
            pdf.setFont("Helvetica", 9)

    pdf.save()

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"ScanMET_Report_{data.get('id', 'scan')}.pdf"
    )
