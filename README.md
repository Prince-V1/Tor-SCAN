# ScanMET v3.1

Frontend: Vercel. Backend: Render.

## Features
- Camera capture + OCR scan
- Batch label scanning (up to 10)
- Google Vision OCR
- Rule-based label screening
- Barcode lookup with Open Food Facts (no API key for normal read requests)
- Optional Gemini explanations
- SQLite history + PDF reports

## Render environment variables
- `GOOGLE_VISION_KEY` = Google Cloud Vision API key
- `GEMINI_API_KEY` = optional Gemini API key from Google AI Studio

Never put either key in the frontend or GitHub. Open Food Facts read lookup uses a custom User-Agent and normally needs no API key.

## Endpoints
- `/scan`
- `/scan-batch`
- `/barcode/{code}`
- `/explain`
- `/history`
- `/report`
- `/health`
