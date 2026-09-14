import csv
import json
import logging
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import pandas as pd
from PIL import Image, ImageOps
import pytesseract
from playwright.sync_api import sync_playwright

def ResolveExecutable(paths):
    for path in paths:
        if path and os.path.exists(path):
            return path
    return None


class AppSettings:
    def __init__(self):
        self.tesseract_path = ResolveExecutable([
            os.environ.get("TESSERACT_PATH"),
            os.environ.get("TESSERACT_EXE"),
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            shutil.which("tesseract"),
        ])

        self.ollama_path = ResolveExecutable([
            os.environ.get("OLLAMA_PATH"),
            os.environ.get("OLLAMA_EXE"),
            os.path.join(os.path.expanduser("~"), "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
            shutil.which("ollama"),
        ])

        self.base_url = os.environ.get("BASE_URL", "https://rpachallengeocr.azurewebsites.net")
        self.upload_url = os.environ.get("UPLOAD_URL", f"{self.base_url}/upload")
        self.csv_path = Path(os.environ.get("CSV_PATH", "Invoices_Extraidos.csv"))
        self.screenshot_path = Path(os.environ.get("SCREENSHOT_PATH", "resultado_upload.png"))

    def apply(self):
        if self.tesseract_path:
            pytesseract.pytesseract.tesseract_cmd = self.tesseract_path
        return self

@dataclass
class Fatura:
    id_fatura: str = ""
    data_vencimento: str = ""
    numero: str = ""
    data_emissao: str = ""
    empresa: str = ""
    valor_total: str = ""

    def ToCsv(self):
        return {
            "ID": self.id_fatura,
            "DueDate": self.data_vencimento,
            "InvoiceNo": self.numero,
            "InvoiceDate": self.data_emissao,
            "CompanyName": self.empresa,
            "TotalDue": self.valor_total,
        }

settings = AppSettings().apply()

TESSERACT_PATH = settings.tesseract_path
OLLAMA_PATH = settings.ollama_path
BASE_URL = settings.base_url
UPLOAD_URL = settings.upload_url
CSV_PATH = settings.csv_path
SCREENSHOT_PATH = settings.screenshot_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("rpa_ocr")

def CleanText(value):
    if value is None:
        return ""
    return str(value).strip()

def DownloadInvoice(invoice_name: str, folder: str = "invoices") -> str:
    if not invoice_name:
        raise ValueError("Nome da fatura não informado.")

    target_dir = Path(folder)
    target_dir.mkdir(exist_ok=True)

    invoice_url = f"{BASE_URL}/invoices/{invoice_name}"
    output_path = target_dir / invoice_name

    logger.info("Baixando invoice: %s", invoice_url)
    try:
        urllib.request.urlretrieve(invoice_url, output_path)
    except Exception as exc:
        raise RuntimeError(f"Falha ao baixar a fatura {invoice_name}: {exc}") from exc

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError(f"Arquivo baixado vazio ou inexistente: {invoice_name}")

    return str(output_path)


def ReadInvoiceWithOcr(invoice_path: str) -> str:
    tess_path = pytesseract.pytesseract.tesseract_cmd or ""
    if not tess_path or not os.path.exists(tess_path):
        raise RuntimeError(
            f"Executável do Tesseract não encontrado em: {tess_path or 'caminho configurado'}\n"
            "Instale o Tesseract e configure pytesseract.pytesseract.tesseract_cmd com o caminho correto do .exe."
        )

    if not os.path.exists(invoice_path):
        raise FileNotFoundError(f"Arquivo da fatura não existe: {invoice_path}")

    try:
        image = Image.open(invoice_path)
        image = ImageOps.grayscale(image)
        image = ImageOps.autocontrast(image)
        width, height = image.size
        if width < 1500 or height < 1000:
            image = image.resize((max(width * 2, 1000), max(height * 2, 1000)), Image.LANCZOS)
        image = image.point(lambda p: 255 if p > 185 else 0)

        configs = [
            "--psm 6 --oem 3",
            "--psm 11 --oem 3",
            "--psm 4 --oem 3",
        ]
        texts = []
        for config in configs:
            text = pytesseract.image_to_string(image, config=config)
            if text and text.strip():
                texts.append(text.strip())

        if texts:
            return max(texts, key=len)
        return ""
    except Exception as exc:
        raise RuntimeError(f"Erro ao ler OCR da fatura {invoice_path}: {exc}") from exc


def ExtractFieldsRegex(text: str):
    cleaned = re.sub(r"\s+", " ", text or "").strip()

    invoice_number = ""
    invoice_date = ""
    company_name = ""
    total_due = ""

    invoice_number_match = re.search(
        r"(?:Invoice\s*(?:No|Number)|Invoice\s*#|INV(?:OIC|O)E?|No\.?|Invoice)\s*[:#-]?\s*([A-Za-z0-9-]+)",
        cleaned,
        flags=re.I,
    )
    if invoice_number_match:
        invoice_number = CleanText(invoice_number_match.group(1))

    invoice_date_match = re.search(
        r"(?:Invoice\s*Date|Date)\s*[:#-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        cleaned,
        flags=re.I,
    )
    if invoice_date_match:
        invoice_date = CleanText(invoice_date_match.group(1))

    company_name_match = re.search(
        r"(?:Company(?:\s*Name)?|Vendor|Customer|From|Issuer)\s*[:#-]?\s*([A-Z][A-Za-z0-9&.\- ]{2,80}?)(?=\s*(?:Bill\s*To|Billed\s*To|Send\s*To|Company\s*Bill|Cliente|Empresa\s*destinatária|Destinatário|To)\b|$)",
        cleaned,
        flags=re.I,
    )
    if company_name_match:
        company_name = CleanText(company_name_match.group(1))

    if not company_name:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        for line in lines[:12]:
            candidate = CleanText(line)
            if not candidate:
                continue
            if re.fullmatch(r"(?:INVOICE|DATE|TOTAL|DUE|BALANCE|PHONE|EMAIL|BILL\s*TO|ADDRESS|USA|USA\.)", candidate, flags=re.I):
                continue
            if re.match(r"^[A-Z][A-Za-z0-9&.'\-]+(?:\s+[A-Z][A-Za-z0-9&.'\-]+){1,5}$", candidate) and len(candidate) <= 80:
                if not re.search(r"\b(?:STREET|ROAD|AVENUE|BLVD|DRIVE|LANE|NUNC|WASHINGTON|USA|PHONE|EMAIL|OFFICE)\b", candidate, flags=re.I):
                    company_name = candidate
                    break

    total_due_match = re.search(
        r"(?:Total\s*Due|Amount\s*Due|Balance\s*Due|TOTAL|Due)\s*[:$]?\s*\$?\s*([0-9,]+(?:\.\d{2})?)",
        cleaned,
        flags=re.I,
    )
    if total_due_match:
        total_due = CleanText(total_due_match.group(1))

    return {
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "company_name": company_name,
        "total_due": total_due,
    }


def AskLocalAi(text: str):
    ollama_binary = OLLAMA_PATH if os.path.exists(OLLAMA_PATH) else shutil.which("ollama")
    if not ollama_binary:
        return None

    prompt = (
        "Você é um assistente de extração de documentos. Extraia apenas os campos seguintes do texto da fatura: "
        "invoice_number, invoice_date, company_name, total_due. Retorne apenas JSON válido. "
        "Importante: company_name é o nome da empresa emissora da fatura, normalmente o bloco no topo do documento, "
        "logo antes ou logo abaixo da palavra INVOICE, ou acima do endereço, telefone e e-mail da empresa. "
        "Não use rua, cidade, estado, telefone, e-mail ou a própria palavra INVOICE como company_name. "
        "Se um campo não for encontrado, use string vazia.\n\n"
        f"TEXTO:\n{text[:5000]}"
    )

    try:
        result = subprocess.run(
            [ollama_binary, "run", "llama3.2:latest", prompt],
            capture_output=True,
            timeout=90,
            check=False,
        )

        stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()

        if result.returncode != 0:
            if stderr:
                print(f"Erro do Ollama stderr: {stderr[:500]}")
            return None

        if not stdout:
            if stderr:
                print(f"Erro do Ollama stderr: {stderr[:500]}")
            return None

        start = stdout.find("{")
        end = stdout.rfind("}")
        if start != -1 and end != -1 and end > start:
            stdout = stdout[start : end + 1]

        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        print(f"Erro ao interpretar JSON da IA local: {exc}")
        return None
    except Exception as exc:
        print(f"Erro da IA local: {exc}")
        return None


def FetchInicialData():
    candidates = [
        ("GET", f"{BASE_URL}/seed"),
        ("POST", f"{BASE_URL}/seed"),
        ("GET", f"{BASE_URL}/api/seed"),
        ("POST", f"{BASE_URL}/api/seed"),
    ]

    for method, url in candidates:
        try:
            data = b"{}" if method == "POST" else None
            request = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method=method,
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
                if not raw:
                    logger.warning("Resposta vazia na seed: %s", url)
                    continue
                payload = json.loads(raw.decode("utf-8", errors="replace"))
                if payload is None:
                    logger.warning("Payload da seed inválido em %s", url)
                    continue
                logger.info("Seed carregada com sucesso em %s", url)
                return payload
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                logger.info("Endpoint não encontrado em %s; tentando próxima rota.", url)
            else:
                logger.warning("Falha ao consultar %s: %s", url, exc)
            continue
        except Exception as exc:
            logger.warning("Falha ao consultar %s: %s", url, exc)
            continue

    raise RuntimeError("Não foi possível obter os dados da API de seed da página.")


def FetchInicialDataFromPage(page):
    try:
        with page.expect_response(lambda response: "seed" in response.url.lower() or "api" in response.url.lower() or "invoice" in response.url.lower()) as response_info:
            pass
    except Exception:
        response_info = None

    try:
        start_button = page.locator('#start')
        print("Botão #start clicado antes do consumo da API.")
        if start_button.count() > 0:
            start_button.first.wait_for(state="visible", timeout=2000)
            with page.expect_response(lambda response: "seed" in response.url.lower() or "api" in response.url.lower() or "invoice" in response.url.lower()) as response_info:
                start_button.first.click()
            response = response_info.value
            payload = response.json()
            logger.info("Payload capturado da response do clique em #start: %s", response.url)
            return payload
    except Exception as exc:
        logger.warning("Não foi possível capturar a response do clique em #start: %s", exc)

    try:
        return FetchInicialData()
    except Exception:
        return None


def NormalizeInvoiceReference(value):
    if not isinstance(value, str):
        return ""

    clean = value.strip()
    if not clean:
        return ""

    if "/invoices/" in clean.lower():
        match = re.search(r"/invoices/([^/?#]+)", clean, re.I)
        if match:
            return match.group(1)

    if re.search(r"\.(jpg|jpeg|png|pdf)$", clean, flags=re.I):
        return clean.split("/")[-1]

    if re.fullmatch(r"\d+\.(jpg|jpeg|png|pdf)", clean, flags=re.I):
        return clean

    if re.fullmatch(r"\d+", clean):
        return f"{clean}.jpg"

    return ""


def ParseDateValue(value):
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def ExtractInvoiceNamesFromData(payload):
    candidates = []

    def walk(obj):
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if isinstance(value, str):
                    normalized = NormalizeInvoiceReference(value)
                    if normalized:
                        candidates.append(normalized)

                if key_l in {"invoice", "invoice_name", "invoicefile", "filename", "file", "file_name", "invoice_url", "invoiceurl", "invoiceimage", "download"}:
                    if isinstance(value, str):
                        normalized = NormalizeInvoiceReference(value)
                        if normalized:
                            candidates.append(normalized)

                if key_l in {"id", "invoice_id", "invoiceid", "row_id"}:
                    if isinstance(value, str):
                        normalized = NormalizeInvoiceReference(value)
                        if normalized:
                            candidates.append(normalized)

                walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)

    unique_names = []
    seen = set()
    for item in candidates:
        clean = item.strip()
        if not clean:
            continue
        if clean.lower().endswith((".jpg", ".jpeg", ".png", ".pdf")):
            normalized = clean
        else:
            normalized = f"{clean}.jpg"
        if normalized not in seen:
            seen.add(normalized)
            unique_names.append(normalized)

    if not unique_names:
        raise RuntimeError("Nenhum nome de invoice válido foi encontrado na resposta da API inicial. O payload parece conter só IDs e não referências de arquivo.")

    return unique_names


def ProcessInvoice(invoice_name: str):
    invoice_path = DownloadInvoice(invoice_name)
    logger.info("=== Leitura OCR: %s ===", invoice_name)
    ocr_text = ReadInvoiceWithOcr(invoice_path)

    result = ExtractFieldsRegex(ocr_text)
    logger.info("Regex extraído: %s", json.dumps(result, ensure_ascii=False, indent=2))

    ai_result = AskLocalAi(ocr_text)
    if ai_result:
        logger.info("IA local retornou: %s", json.dumps(ai_result, ensure_ascii=False, indent=2))
    else:
        logger.warning("IA local indisponível ou falhou; usando regex como fallback.")

    return {"invoice_name": invoice_name, "ocr": ocr_text, "regex": result, "ai": ai_result}


def ExtractDueDateAndIdFromData(payload):
    rows = []

    def walk(obj):
        if isinstance(obj, dict):
            current = {}
            for key, value in obj.items():
                key_l = str(key).lower()
                if key_l in {"id", "invoice_id", "invoiceid", "row_id"} and isinstance(value, str):
                    current["id"] = value.strip()
                if key_l in {"due_date", "duedate", "due", "date_due"} and isinstance(value, str):
                    current["due_date"] = value.strip()
                if key_l in {"invoice", "invoice_name", "invoicefile", "filename", "file", "file_name", "invoice_url", "invoiceurl"} and isinstance(value, str):
                    current["invoice_file"] = NormalizeInvoiceReference(value)
                walk(value)

            if current:
                rows.append(current)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(payload)

    unique_rows = []
    seen = set()
    for item in rows:
        key = (item.get("id", ""), item.get("due_date", ""), item.get("invoice_file", ""))
        key_str = "|".join(key)
        if key_str and key_str not in seen:
            seen.add(key_str)
            unique_rows.append(item)

    return unique_rows


def ExportRowsToCsv(rows, output_path="Invoices_Extraidos.csv"):
    if not rows:
        invoices_data = []
    else:
        invoices_data = [
            [
                row.get("ID", ""),
                row.get("DueDate", ""),
                row.get("InvoiceNo", ""),
                row.get("InvoiceDate", ""),
                row.get("CompanyName", ""),
                row.get("TotalDue", ""),
            ]
            for row in rows
        ]

    df = pd.DataFrame(invoices_data, columns=["ID", "DueDate", "InvoiceNo", "InvoiceDate", "CompanyName", "TotalDue"])

    if not df.empty and "DueDate" in df.columns:
        df["DueDate"] = pd.to_datetime(df["DueDate"], dayfirst=True, errors="coerce").dt.strftime("%d-%m-%Y")
    if not df.empty and "InvoiceDate" in df.columns:
        df["InvoiceDate"] = pd.to_datetime(df["InvoiceDate"], dayfirst=True, errors="coerce").dt.strftime("%d-%m-%Y")

    df.to_csv(output_path, index=False)
    print(f"\n=== CSV exportado em: {output_path} ===")
    return output_path


def UploadCsvWithPlaywright(csv_path: str, page=None):
    try:

        file_input = page.locator('input[type="file"][name="csv"]')
        file_input.set_input_files(csv_path)
        print(f"Arquivo enviado via Playwright: {csv_path}")

        page.wait_for_timeout(1000)
        page.screenshot(path=str(SCREENSHOT_PATH), full_page=True)
        print(f"Captura salva em: {SCREENSHOT_PATH}")
        return True
    except Exception as exc:
        print(f"Falha ao enviar arquivo via Playwright: {exc}")
        return False


def ProcessAllInvoicesFromData():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        page = browser.new_page(viewport={"width": 1920, "height": 1080})
        page.goto(BASE_URL, wait_until="domcontentloaded")
        print(f"Site aberto antes da API: {BASE_URL}")
        page.wait_for_timeout(3000)
        payload = FetchInicialDataFromPage(page)
        invoice_names = ExtractInvoiceNamesFromData(payload)
        rows_meta = ExtractDueDateAndIdFromData(payload)

        if not invoice_names:
            raise RuntimeError("Nenhum ID ou nome de fatura foi encontrado na resposta da API.")

        logger.info("Total de faturas encontradas na base de dados inicial: %s", len(invoice_names))
        extracted_rows = []
        today = date.today()
        ignored = 0
        failed = 0

        for index, invoice_name in enumerate(invoice_names, start=1):
            logger.info("### Processando fatura %s/%s: %s", index, len(invoice_names), invoice_name)
            try:
                result = ProcessInvoice(invoice_name)

                regex_values = result.get("regex", {}) or {}
                ai_values = result.get("ai", {}) or {}

                def value_for(primary, fallback_key, fallback_value):
                    if isinstance(primary, dict) and primary.get(fallback_key):
                        return primary.get(fallback_key)
                    return fallback_value

                meta = next((item for item in rows_meta if item.get("invoice_file") == invoice_name or item.get("id") == invoice_name or item.get("invoice_file") == invoice_name.replace(".jpg", "")), {})

                due_date_raw = meta.get("due_date", "")
                due_date_obj = ParseDateValue(due_date_raw)

                if due_date_obj is None or due_date_obj >= today:
                    logger.info("Ignorando fatura fora do filtro: %s | vencimento %s", invoice_name, due_date_raw)
                    ignored += 1
                    continue

                invoice_no = value_for(ai_values, "invoice_number", regex_values.get("invoice_number", ""))
                invoice_date = value_for(ai_values, "invoice_date", regex_values.get("invoice_date", ""))
                company_name = value_for(ai_values, "company_name", regex_values.get("company_name", ""))
                total_due = value_for(ai_values, "total_due", regex_values.get("total_due", ""))

                if not invoice_date:
                    invoice_date = regex_values.get("invoice_date", "")

                invoice_date_obj = ParseDateValue(invoice_date)
                if invoice_date_obj:
                    invoice_date_text = invoice_date_obj.strftime("%d-%m-%Y")
                else:
                    raw_invoice_date = CleanText(invoice_date)
                    invoice_date_text = raw_invoice_date
                    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", raw_invoice_date):
                        d, m, y = re.split(r"[/-]", raw_invoice_date)
                        invoice_date_text = f"{int(d):02d}-{int(m):02d}-{int(y):04d}"

                if due_date_obj:
                    due_date_text = due_date_obj.strftime("%d-%m-%Y")
                else:
                    raw_due_date = CleanText(due_date_raw)
                    due_date_text = raw_due_date
                    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", raw_due_date):
                        d, m, y = re.split(r"[/-]", raw_due_date)
                        due_date_text = f"{int(d):02d}-{int(m):02d}-{int(y):04d}"

                fatura = Fatura(
                    id_fatura=CleanText(meta.get("id", "")),
                    data_vencimento=CleanText(due_date_text),
                    numero=CleanText(invoice_no),
                    data_emissao=CleanText(invoice_date_text),
                    empresa=CleanText(company_name),
                    valor_total=CleanText(total_due),
                )
                extracted_rows.append(fatura.ToCsv())
            except Exception as exc:
                failed += 1
                logger.exception("Falha ao processar a fatura %s: %s", invoice_name, exc)
                continue

        logger.info("Processadas: %s | ignoradas: %s | falhas: %s", len(extracted_rows), ignored, failed)
        csv_path = ExportRowsToCsv(extracted_rows)
        UploadCsvWithPlaywright(csv_path, page)
        print("Aguardando 10 segundos antes de finalizar...")
        page.wait_for_timeout(10000)
        return {"rows": extracted_rows, "csv_path": csv_path}


if __name__ == "__main__":
    ProcessAllInvoicesFromData()
