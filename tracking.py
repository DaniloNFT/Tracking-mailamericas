# tracking.py — listo para GitHub Actions (sin archivo .json)
import os, sys, re, json, pytz, time, warnings
from datetime import datetime
from dotenv import load_dotenv

# Google Sheets
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# Selenium estándar (Selenium Manager)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

warnings.filterwarnings("ignore", category=DeprecationWarning)
load_dotenv()  # permite usar .env en local; en Actions vienen de secrets

# ===== Config (todo desde ENV/Secrets) =====
SHEET_ID         = os.getenv("SHEET_ID")
TAB_TRACKING     = os.getenv("TAB_TRACKING", "Tracking")

# Headless por defecto en CI (puedes poner false en local si quieres ver la ventana)
RUN_HEADLESS     = (os.getenv("RUN_HEADLESS", "true").strip().lower() in {"1","true","yes","y"})
PAGELOAD_TIMEOUT = int(os.getenv("PAGELOAD_TIMEOUT", "25"))
IMPLICIT_WAIT    = int(os.getenv("IMPLICIT_WAIT", "10"))

# Si en el workflow instalas Chrome, pasa su ruta en CHROME_BINARY
CHROME_BINARY    = (os.getenv("CHROME_BINARY") or "").strip()

LA_PAZ = pytz.timezone("America/La_Paz")

# Mapeo columnas (A..H) — 1-based
COL_CONTENT = 1  # A: Contenido (manual)
COL_CODE    = 2  # B: Código
COL_STATUS  = 3  # C: Último estado
COL_DATE    = 4  # D: Fecha del estado
COL_CARRIER = 5  # E: Carrier / Ubicación
COL_UPDATED = 6  # F: Última actualización
COL_OBS     = 7  # G: Observación
COL_DONE    = 8  # H: Control ("OK" = omitir)

def now_bo():
    return datetime.now(LA_PAZ).strftime("%Y-%m-%d %H:%M:%S %z")

def creds_from_env():
    """
    Prioriza GOOGLE_SERVICE_ACCOUNT_JSON (contenido del JSON como string, desde secrets).
    Si no existe, intenta GOOGLE_APPLICATION_CREDENTIALS (ruta a archivo).
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    json_inline = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()
    file_path   = (os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or "").strip()

    if json_inline:
        data = json.loads(json_inline)
        return ServiceAccountCredentials.from_json_keyfile_dict(data, scopes=scopes)
    if file_path:
        return ServiceAccountCredentials.from_json_keyfile_name(file_path, scopes=scopes)
    raise RuntimeError("Faltan credenciales: define GOOGLE_SERVICE_ACCOUNT_JSON o GOOGLE_APPLICATION_CREDENTIALS")

def open_ws():
    if not SHEET_ID:
        print("SHEET_ID no definido", file=sys.stderr); sys.exit(2)
    gc = gspread.authorize(creds_from_env())
    return gc.open_by_key(SHEET_ID).worksheet(TAB_TRACKING)

def build_driver():
    opts = ChromeOptions()
    if RUN_HEADLESS:
        # usa headless clásico por máxima compatibilidad en CI
        opts.add_argument("--headless")
        opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,2000")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--disable-background-networking")
    if CHROME_BINARY:
        opts.binary_location = CHROME_BINARY

    d = webdriver.Chrome(options=opts)  # Selenium Manager resuelve el driver
    d.set_page_load_timeout(PAGELOAD_TIMEOUT)
    d.implicitly_wait(IMPLICIT_WAIT)
    return d

def tiny_sleep(): time.sleep(1.2)

# ---------- helpers scraping ----------
def _collect_texts(driver, code: str):
    texts = []
    for css in [
        "div[class*='result']","div[class*='tracking']","div[class*='event']",
        "section","table","tbody","tr","li","p"
    ]:
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, css):
                t = (el.text or "").strip()
                if t and (code[:6] in t or len(t) > 20):
                    texts.append(t)
        except Exception:
            pass
    return texts

def _infer_status_when_carrier(texts):
    status_candidates = [
        "Delivered","Entregado","En tránsito","In Transit","Out for delivery",
        "Llegó a","Despachado","Salida","Arribo","Procesado",
        "Información recibida","Label created","Recibido por Distribuidor"
    ]
    status = when = carrier = None
    for t in texts:
        if not status:
            for c in status_candidates:
                if c.lower() in t.lower():
                    status = c; break
        if status:
            m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})", t)
            when = m.group(1) if m else None
            for k in ["Correo","UPS","DHL","USPS","Bolivia","MailAmericas",
                      "La Paz","Santa Cruz","Cochabamba"]:
                if k.lower() in t.lower():
                    carrier = k; break
            break
    return status or "Sin clasificar", when, carrier

# ---------- extractor principal: último evento ----------
def fetch_status_mailamericas(driver, code: str):
    url = f"https://www.mailamericas.com/tracking?tracking={code}"
    driver.get(url)
    tiny_sleep()

    try:
        timeline = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.process-vertical"))
        )
        steps = timeline.find_elements(By.CSS_SELECTOR, "div.process-step")
        if not steps:
            raise RuntimeError("No hay steps .process-step")

        for step in steps:
            try:
                left  = step.find_element(By.CSS_SELECTOR, "div.process-step-content div.form-row div.col-md-7")
                right = step.find_element(By.CSS_SELECTOR, "div.process-step-content div.form-row div.col-md-5")
            except Exception:
                continue

            # título/estado
            title = ""
            for sel in ["p.h6", "p.font-weight-bold", "p.text-md-left.h6", "p"]:
                try:
                    el = left.find_element(By.CSS_SELECTOR, sel)
                    title = (el.text or "").strip()
                    if title: break
                except Exception:
                    pass
            if not title:
                continue

            # descripción
            observation = ""
            try:
                ps = left.find_elements(By.CSS_SELECTOR, "p")
                if len(ps) >= 2:
                    observation = (ps[1].text or "").strip()
            except Exception:
                pass

            # fecha/hora
            when = ""
            try:
                for el in right.find_elements(By.CSS_SELECTOR, "span, time, p, div"):
                    t = (el.text or "").strip()
                    if len(t) >= 8:
                        when = t; break
            except Exception:
                pass

            carrier = "MailAmericas / Correo destino"
            return title, when, carrier, observation[:900]

        raise RuntimeError("No se pudo identificar el bloque de estado")

    except Exception:
        texts = _collect_texts(driver, code)
        if not texts:
            try:
                with open("last_html.html","w",encoding="utf-8") as f: f.write(driver.page_source)
                driver.save_screenshot("last_page.png")
            except Exception:
                pass
            return None, None, None, "Sin resultados visibles"

        status, when, carrier = _infer_status_when_carrier(texts)
        return status, when, carrier, " | ".join(texts)[:900]

# ---------- main ----------
def main():
    ws = open_ws()
    rows = ws.get_all_values()  # fila 1 = cabecera

    d = build_driver()
    try:
        for i, row in enumerate(rows[1:], start=2):
            code = (row[COL_CODE-1] if len(row) >= COL_CODE else "").strip()
            done = (row[COL_DONE-1] if len(row) >= COL_DONE else "").strip().lower()

            # omitir si no hay código o si marcaste "OK"
            if not code or done == "ok":
                continue

            try:
                status, when, carrier, obs = fetch_status_mailamericas(d, code)
                # escribe C..G
                ws.update(
                    values=[[status or "", when or "", carrier or "", now_bo(), obs or ""]],
                    range_name=f"{chr(64+COL_STATUS)}{i}:{chr(64+COL_OBS)}{i}"
                )
            except Exception as e:
                ws.update(
                    values=[[now_bo(), f"Error {type(e).__name__}"]],
                    range_name=f"{chr(64+COL_UPDATED)}{i}:{chr(64+COL_OBS)}{i}"
                )
    finally:
        try: d.quit()
        except Exception: pass

if __name__ == "__main__":
    main()
